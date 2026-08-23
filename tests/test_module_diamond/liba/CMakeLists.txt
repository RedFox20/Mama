cmake_minimum_required(VERSION 3.25)
project(LibA CXX)

include(mama.cmake)
include_directories(${MAMA_INCLUDES})

add_library(LibA STATIC src/liba/api.cpp)
target_include_directories(LibA PUBLIC ${CMAKE_CURRENT_SOURCE_DIR}/src)
target_link_libraries(LibA ${MAMA_LIBS})

# PRIVATE: this archive compiles shared.api into itself, and so does its sibling. Both then define
# the same module initializer, which collides at the link unless mama strips the exported copies.
mama_target_modules(LibA PRIVATE)

install(TARGETS LibA ARCHIVE DESTINATION lib)
