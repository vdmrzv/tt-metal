set(Python3_FIND_STRATEGY LOCATION)
find_package(Python3 REQUIRED COMPONENTS Development)

add_subdirectory(unit_tests/gtests)
add_subdirectory(benchmark/cpp)
