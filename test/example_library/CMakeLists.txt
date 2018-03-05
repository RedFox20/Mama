cmake_minimum_required(VERSION 3.6)
project(example_library)


file(GLOB_RECURSE EXAMPLE_LIBRARY_SOURCES *.cpp *.h)
source_group(ExampleLibrary FILES ${EXAMPLE_LIBRARY_SOURCES})
add_library(ExampleLibrary STATIC ${EXAMPLE_LIBRARY_SOURCES})

install(TARGETS ExampleLibrary DESTINATION ${CMAKE_CURRENT_SOURCE_DIR}/bin)
