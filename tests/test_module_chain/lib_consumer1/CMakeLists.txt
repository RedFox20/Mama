cmake_minimum_required(VERSION 3.25)
project(LibConsumer1 CXX)

include(mama.cmake)
include_directories(${MAMA_INCLUDES})

add_library(LibConsumer1 STATIC src/lib1/api.cpp)
target_include_directories(LibConsumer1 PUBLIC ${CMAKE_CURRENT_SOURCE_DIR}/src)
target_link_libraries(LibConsumer1 ${MAMA_LIBS})

# PRIVATE: this archive compiles the modules of every package below it, which is what makes its own
# published archive carry their initializers unless mama strips them.
mama_target_modules(LibConsumer1 PRIVATE)

if(MAMA_HAS_MODULES)
    target_sources(LibConsumer1 PUBLIC FILE_SET lib1_modules TYPE CXX_MODULES
                   BASE_DIRS ${CMAKE_CURRENT_SOURCE_DIR}/src FILES src/lib1/lib1-api.cppm)
    install(TARGETS LibConsumer1 ARCHIVE DESTINATION lib FILE_SET lib1_modules DESTINATION src)
else()
    install(TARGETS LibConsumer1 ARCHIVE DESTINATION lib)
endif()
