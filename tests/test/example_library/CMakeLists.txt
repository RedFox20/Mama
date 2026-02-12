cmake_minimum_required(VERSION 3.20)
project(example_library)

set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS ON)

file(GLOB_RECURSE EXAMPLE_LIBRARY_SOURCES *.cpp *.h)
source_group(ExampleLibrary FILES ${EXAMPLE_LIBRARY_SOURCES})
add_library(ExampleLibrary STATIC ${EXAMPLE_LIBRARY_SOURCES})

install(TARGETS ExampleLibrary DESTINATION bin)
