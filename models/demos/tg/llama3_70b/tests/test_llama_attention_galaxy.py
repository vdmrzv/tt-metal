# SPDX-FileCopyrightText: © 2023 Tenstorrent Inc.

# SPDX-License-Identifier: Apache-2.0

import gc

import pytest
import torch
from loguru import logger

import ttnn
from models.demos.t3000.llama2_70b.reference.llama.llama import Llama
from models.demos.t3000.llama2_70b.reference.llama.llama.model import precompute_freqs_cis
from models.demos.t3000.llama2_70b.tt.llama_common import (
    BASE_URL,
    MAX_SEQ_LEN,
    MAX_SEQ_LEN_LLAMA3,
    UNIT_TEST_GENERATION_LENGTH,
    UNIT_TEST_LAYER_NUM,
    UNIT_TEST_N_LAYER,
    UNIT_TEST_START_POS,
    check_kv_cache,
    check_mesh_device,
    comp_pcc,
    extract_pcc_from_log,
    gather_cos_sin,
    generate_rot_emb,
    get_rot_transformation_mat,
    get_rotation_mat,
    num_to_corerange,
    precompute_freqs,
    should_skip_model_load,
)
from models.demos.tg.llama3_70b.tt.llama_attention_galaxy import TtLlamaAttention_galaxy
from models.demos.tg.llama3_70b.tt.llama_common import setup_llama_env
from models.utility_functions import skip_for_grayskull
from ttnn import ReplicateTensorToMesh


class PytorchLlamaAttentionModel(torch.nn.Module):
    def __init__(self, hf_reference_model, layer_num, rope_theta):
        super().__init__()
        self.attention = hf_reference_model.layers[layer_num].attention
        self.rope_theta = rope_theta
        # Disable dropout
        self.attention.eval()

        configuration = hf_reference_model.params
        self.n_heads = configuration.n_heads
        hidden_dim = configuration.dim
        self.head_dim = hidden_dim // self.n_heads
        self.max_seq_len = configuration.max_seq_len

    def prepare_inputs(self, x, start_pos):
        """
        Prepare inputs for decode mode. Assume that current token is at
        start_pos, and KV cache has valid data up to start_pos.
        """
        batch = x.size(0)
        freqs_cis = precompute_freqs_cis(self.head_dim, self.max_seq_len * 2, self.rope_theta)
        freqs_cis = freqs_cis[start_pos : start_pos + 1]

        attn_mask = torch.zeros(batch, 1, 1, start_pos + 1)
        # attn_mask[:, :, :, : start_pos + 1] = -1e9
        attn_mask = attn_mask.expand(-1, self.n_heads, -1, -1)

        return x, start_pos, freqs_cis, attn_mask

    def prepare_inputs_prefill(self, x, start_pos):
        """
        Prepare inputs for decode mode. Assume that current token is at
        start_pos, and KV cache has valid data up to start_pos.
        """
        batch = x.size(0)
        seq_len = x.size(1)
        freqs_cis = precompute_freqs_cis(self.head_dim, self.max_seq_len * 2, self.rope_theta)
        freqs_cis = freqs_cis[start_pos : start_pos + seq_len]

        attn_mask = torch.full((seq_len, seq_len), float("-inf"))
        attn_mask = torch.triu(attn_mask, diagonal=1)
        attn_mask = attn_mask.expand(batch, self.n_heads, -1, -1)

        return x, start_pos, freqs_cis, attn_mask

    def forward(self, x, start_pos, freqs_cis, mask):
        """
        x: (batch, seq, hidden_dim)
        start_pos: int
        freqs_cis: ?
        mask: ?

        return: (batch, seq, hidden_dim)
        """
        result = self.attention(
            x,
            start_pos,
            freqs_cis,
            mask,
        )
        return result


def tt_llama_attention_prepare_inputs(llama_attention_model, x, start_pos, rope_theta, mode="decode"):
    assert len(x.size()) == 3
    batch, seq_len, _ = x.shape

    cache_name = lambda name: llama_attention_model.cache_path / (f"{name}")

    if mode == "decode":
        assert seq_len == 1, "Only supporting decode mode"
        x = x.transpose(0, 1).unsqueeze(1)
        assert x.shape == (seq_len, 1, batch, llama_attention_model.hidden_size)

        ACT_MEMCFG = ttnn.create_sharded_memory_config(
            shape=(x.shape[2], x.shape[3] // 32 // llama_attention_model.cluster_shape[0]),
            core_grid=ttnn.CoreGrid(y=4, x=8),
            strategy=ttnn.ShardStrategy.WIDTH,
            orientation=ttnn.ShardOrientation.ROW_MAJOR,
            use_height_and_width_as_shard_shape=True,
        )
        xs = ttnn.as_tensor(
            x,
            dtype=ttnn.bfloat16,
            layout=ttnn.TILE_LAYOUT,
            memory_config=ACT_MEMCFG,
            device=llama_attention_model.mesh_device,
            mesh_mapper=ttnn.ShardTensor2dMesh(
                llama_attention_model.mesh_device,
                mesh_shape=tuple(reversed(llama_attention_model.cluster_shape)),
                dims=(None, 3),
            ),
        )

        batch_size_per_group = llama_attention_model.batch_size_per_device_group

        rot_emb = generate_rot_emb(llama_attention_model.head_dim, llama_attention_model.max_seq_len * 2, rope_theta)
        rot_mat = get_rotation_mat(rot_emb, start_pos, seq_len, batch=batch_size_per_group)
        assert rot_mat.size() == (
            1,
            batch_size_per_group,
            llama_attention_model.head_dim,
            llama_attention_model.head_dim,
        )

        shard_spec_n_cores_grid = ttnn.CoreRangeSet({num_to_corerange(batch_size_per_group)})
        ROT_MAT_MEMCFG = ttnn.MemoryConfig(
            ttnn.TensorMemoryLayout.HEIGHT_SHARDED,
            ttnn.BufferType.L1,
            ttnn.ShardSpec(
                shard_spec_n_cores_grid,
                [
                    llama_attention_model.head_dim,
                    llama_attention_model.head_dim,
                ],
                ttnn.ShardOrientation.ROW_MAJOR,
            ),
        )
        rot_mats = ttnn.as_tensor(
            rot_mat,
            dtype=ttnn.bfloat16,
            layout=ttnn.TILE_LAYOUT,
            memory_config=ROT_MAT_MEMCFG,
            device=llama_attention_model.mesh_device,
            mesh_mapper=ReplicateTensorToMesh(llama_attention_model.mesh_device),
        )

        attn_masks = None

    elif mode == "prefill":
        assert (
            seq_len % 256 == 0 and seq_len > 0 and seq_len <= 8192
        ), "Prefill mode only supports seqlen as a multiple of 256 up to 8k"
        assert batch == 1, "prefill mode only supports batch size 1"
        x = x.unsqueeze(0)
        assert x.shape == (1, batch, seq_len, llama_attention_model.hidden_size)
        xs = ttnn.as_tensor(
            x,
            dtype=ttnn.bfloat16,
            layout=ttnn.TILE_LAYOUT,
            memory_config=ttnn.DRAM_MEMORY_CONFIG,
            device=llama_attention_model.mesh_device,
            mesh_mapper=ttnn.ShardTensor2dMesh(
                llama_attention_model.mesh_device,
                mesh_shape=tuple(reversed(llama_attention_model.cluster_shape)),
                dims=(None, 3),
            ),
        )

        cos, sin = precompute_freqs(
            llama_attention_model.head_dim, llama_attention_model.max_seq_len * 2, rope_theta, use_scaled=False
        )
        cos_gathered, sin_gathered = gather_cos_sin(torch.arange(start_pos, start_pos + seq_len), cos, sin)
        assert cos_gathered.size() == (1, 1, seq_len, llama_attention_model.head_dim)
        assert sin_gathered.size() == (1, 1, seq_len, llama_attention_model.head_dim)

        cos_gathereds = ttnn.as_tensor(
            cos_gathered,
            dtype=ttnn.bfloat16,
            layout=ttnn.TILE_LAYOUT,
            # cache_file_name=cache_name(f"cos_gathered_prefill_{seq_len}"),
            memory_config=ttnn.DRAM_MEMORY_CONFIG,
            device=llama_attention_model.mesh_device,
            mesh_mapper=ReplicateTensorToMesh(llama_attention_model.mesh_device),
        )
        sin_gathereds = ttnn.as_tensor(
            sin_gathered,
            dtype=ttnn.bfloat16,
            layout=ttnn.TILE_LAYOUT,
            # cache_file_name=cache_name(f"sin_gathered_prefill_{seq_len}"),
            memory_config=ttnn.DRAM_MEMORY_CONFIG,
            device=llama_attention_model.mesh_device,
            mesh_mapper=ReplicateTensorToMesh(llama_attention_model.mesh_device),
        )

        rot_mats = [cos_gathereds, sin_gathereds]

        attn_mask = torch.full((seq_len, seq_len), torch.finfo(torch.float32).min)
        attn_mask = torch.triu(attn_mask, diagonal=1)
        attn_mask = attn_mask.expand(1, batch, -1, -1)
        attn_masks = ttnn.as_tensor(
            attn_mask,
            dtype=ttnn.bfloat16,
            layout=ttnn.TILE_LAYOUT,
            # cache_file_name=cache_name(f"attn_mask_prefill_{seq_len}"),
            mesh_mapper=ReplicateTensorToMesh(llama_attention_model.mesh_device),
            memory_config=ttnn.DRAM_MEMORY_CONFIG,
            device=llama_attention_model.mesh_device,
        )

    return (
        xs,
        start_pos,
        rot_mats,
        attn_masks,
    )


def run_test_LlamaAttention_inference(
    mesh_device,
    cluster_shape,
    batch,
    seq_len,
    pcc,
    model_config,
    llama_version,
    ckpt_dir,
    tokenizer_path,
    cache_path,
):
    # Prepare paths and devices
    skip_model_load = should_skip_model_load()

    # Prepare configs
    hugging_face_reference_model = Llama.build(
        ckpt_dir,
        tokenizer_path,
        max_seq_len=MAX_SEQ_LEN if llama_version == "llama2" else MAX_SEQ_LEN_LLAMA3,
        max_batch_size=batch,
        n_layers=UNIT_TEST_N_LAYER,
        skip_model_load=skip_model_load,
    ).model
    hugging_face_reference_model.eval()
    state_dict = hugging_face_reference_model.state_dict()
    logger.info(state_dict.keys())
    torch.manual_seed(0)
    configuration = hugging_face_reference_model.params

    # PyTorch model --------------------------------------------------------------------
    pytorch_LlamaAttention_model = PytorchLlamaAttentionModel(
        hugging_face_reference_model, UNIT_TEST_LAYER_NUM, configuration.rope_theta
    )
    # TT model -------------------------------------------------------------------------
    transformation_mat_torch = get_rot_transformation_mat(32)  # 32 for tile size

    transformation_mats = ttnn.as_tensor(
        transformation_mat_torch,
        dtype=ttnn.bfloat16,
        layout=ttnn.TILE_LAYOUT,
        memory_config=ttnn.DRAM_MEMORY_CONFIG,
        device=mesh_device,
        mesh_mapper=ReplicateTensorToMesh(mesh_device),
    )

    tt_LlamaAttention_model = TtLlamaAttention_galaxy(
        mesh_device,
        cluster_shape,
        state_dict,
        BASE_URL,
        UNIT_TEST_LAYER_NUM,
        model_config,
        configuration,
        transformation_mats,
        cache_path=cache_path,
    )

    mode = "decode" if seq_len == 1 else "prefill"

    all_tests_pass, all_pccs = True, []
    if mode == "prefill":
        generation_start_pos = 0
        generation_length = 1
    else:
        generation_start_pos = UNIT_TEST_START_POS
        generation_length = UNIT_TEST_GENERATION_LENGTH

    for i in range(generation_length):
        # Prepare input
        pt_inp_ids = torch.randint(0, configuration.vocab_size, (batch, seq_len))
        pt_inp = hugging_face_reference_model.tok_embeddings(pt_inp_ids)
        pt_inp_normed = hugging_face_reference_model.layers[UNIT_TEST_LAYER_NUM].attention_norm(pt_inp)
        tt_input = pt_inp_normed.clone()
        start_pos = generation_start_pos + i

        # PyTorch output --------------------------------------------------------------------
        if mode == "prefill":
            attention_input, start_pos, freqs_cis, attn_mask = pytorch_LlamaAttention_model.prepare_inputs_prefill(
                pt_inp_normed, start_pos
            )
        else:
            attention_input, start_pos, freqs_cis, attn_mask = pytorch_LlamaAttention_model.prepare_inputs(
                pt_inp_normed, start_pos
            )

        pytorch_out = pytorch_LlamaAttention_model(
            attention_input,
            start_pos,
            freqs_cis,
            attn_mask,
        )

        # TT hardware execution -------------------------------------------------------------
        attention_input, start_pos, rot_mat, attn_mask = tt_llama_attention_prepare_inputs(
            tt_LlamaAttention_model, tt_input, start_pos, configuration.rope_theta, mode=mode
        )
        tt_out = tt_LlamaAttention_model(
            attention_input,
            rot_mat,
            start_pos,
            attn_mask,
            mode=mode,
        )
        # tt_out = [ttnn.to_torch(shard) for shard in ttnn.get_device_tensors(tt_out.cpu())]

        tt_out = ttnn.to_torch(
            tt_out,
            mesh_composer=ttnn.ConcatMesh2dToTensor(
                mesh_device, mesh_shape=tuple(reversed(cluster_shape)), dims=(1, 3)
            ),
        )
        tt_out = tt_out[:, 0:1, :, :]
        tt_out = tt_out.permute(2, 1, 0, 3).squeeze(1)  # [seq, batch, hidden_dim]

        does_pass, output_pcc = comp_pcc(pytorch_out, tt_out, pcc)
        logger.info(f"Output: {output_pcc}")

        all_pccs.append(extract_pcc_from_log(output_pcc))

        if does_pass:
            logger.info(f"[start_pos={start_pos}] {llama_version} Attention output Passed!")
        else:
            logger.warning(
                f"[start_pos={start_pos}] {llama_version} Attention output Failed! PCC value is lower than {pcc}"
            )
            all_tests_pass = False

    logger.info(f"Average PCC over {len(all_pccs)} tokens: {sum(all_pccs) / len(all_pccs)}")

    # Check kv cache
    # PyTorch output --------------------------------------------------------------------
    pytorch_layer_present = [
        pytorch_LlamaAttention_model.attention.cache_k.clone().permute(0, 2, 1, 3)[
            :batch, ...
        ],  # [batch, n_kv_heads, seq, head_dim]
        pytorch_LlamaAttention_model.attention.cache_v.clone().permute(0, 2, 1, 3)[
            :batch, ...
        ],  # [batch, n_kv_heads, seq, head_dim]
    ]
    # TT hardware output ----------------------------------------------------------------

    # concat the pasts by heads
    tt_layer_present_all = [ttnn.from_device(lp) for lp in tt_LlamaAttention_model.layer_past]

    tt_layer_present_all = [
        ttnn.to_torch(
            lp,
            mesh_composer=ttnn.ConcatMesh2DToTensor(
                mesh_device, mesh_shape=tuple(reversed(cluster_shape)), dims=(1, 0)
            ),
        )[:batch, ...]
        for lp in tt_layer_present_all
    ]

    cache_test_pass = check_kv_cache(
        pytorch_layer_present,
        tt_layer_present_all,
        generation_start_pos,
        generation_length,
        seq_len,
        mode == "prefill",
        pcc,
    )

    all_tests_pass = all_tests_pass and cache_test_pass

    if all_tests_pass:
        logger.info(f"{llama_version} Attention output Passed!")
    else:
        gc.collect()
        logger.warning(f"{llama_version} Attention output Failed!")
        assert all_tests_pass, f"PCC value is lower than {pcc} for some of the outputs. Check Warnings!"


@skip_for_grayskull("Requires eth connected devices to run")
@pytest.mark.parametrize(
    "cluster_shape, mesh_device", [pytest.param((4, 8), (8, 4), id="4x8_grid")], indirect=["mesh_device"]
)
@pytest.mark.parametrize(
    "llama_version",
    (("llama3-tg"),),
)
@pytest.mark.parametrize(
    "batch, seq_len, pcc",
    [
        (32, 1, 0.9995),
        (1, 256, 0.999),
    ],
    ids=[
        "decode",
        "prefill",
    ],
)
@pytest.mark.parametrize(
    "max_batch_size, max_context_len",
    (
        (32, 2048),
        # (16, 8192),
    ),
    ids=(
        "short_context",
        # "long_context",
    ),
)
def test_LlamaAttention_inference(
    batch,
    seq_len,
    pcc,
    mesh_device,
    max_batch_size,
    max_context_len,
    llama_version,
    cluster_shape,
    use_program_cache,
):
    if batch > max_batch_size:
        pytest.skip(f"Decode with {batch} users is not supported with large context")

    if batch == 1 and seq_len > max_context_len:
        pytest.skip(f"Prefill with {seq_len=} is not supported with short context")

    if llama_version == "llama2" and seq_len > 2048:
        pytest.skip(f"Llama2 with {seq_len=} is not supported (max 2048)")

    model_config, ckpt_dir, tokenizer_path, cache_path = setup_llama_env(
        llama_version=llama_version,
        max_batch_size=max_batch_size,
        max_context_len=max_context_len,
    )
    check_mesh_device(mesh_device, model_config)
    run_test_LlamaAttention_inference(
        mesh_device,
        cluster_shape,
        batch,
        seq_len,
        pcc,
        model_config,
        llama_version,
        ckpt_dir,
        tokenizer_path,
        cache_path,
    )
