cmake_minimum_required(VERSION 3.25)
project(LibB CXX)

include(mama.cmake)
include_directories(${MAMA_INCLUDES})

add_library(LibB STATIC src/libb/api.cpp)
target_include_directories(LibB PUBLIC ${CMAKE_CURRENT_SOURCE_DIR}/src)
target_link_libraries(LibB ${MAMA_LIBS})

# PRIVATE: this archive compiles shared.api into itself, and so does its sibling. Both then define
# the same module initializer, which collides at the link unless mama strips the exported copies.
mama_target_modules(LibB PRIVATE)

install(TARGETS LibB ARCHIVE DESTINATION lib)
