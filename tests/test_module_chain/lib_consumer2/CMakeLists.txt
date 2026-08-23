cmake_minimum_required(VERSION 3.25)
project(LibConsumer2 CXX)

include(mama.cmake)
include_directories(${MAMA_INCLUDES})

add_library(LibConsumer2 STATIC src/lib2/api.cpp)
target_include_directories(LibConsumer2 PUBLIC ${CMAKE_CURRENT_SOURCE_DIR}/src)
target_link_libraries(LibConsumer2 ${MAMA_LIBS})

# PRIVATE: this archive compiles the modules of every package below it, which is what makes its own
# published archive carry their initializers unless mama strips them.
mama_target_modules(LibConsumer2 PRIVATE)

if(MAMA_HAS_MODULES)
    target_sources(LibConsumer2 PUBLIC FILE_SET lib2_modules TYPE CXX_MODULES
                   BASE_DIRS ${CMAKE_CURRENT_SOURCE_DIR}/src FILES src/lib2/lib2-api.cppm)
    install(TARGETS LibConsumer2 ARCHIVE DESTINATION lib FILE_SET lib2_modules DESTINATION src)
else()
    install(TARGETS LibConsumer2 ARCHIVE DESTINATION lib)
endif()
