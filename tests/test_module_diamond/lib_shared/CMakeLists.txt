cmake_minimum_required(VERSION 3.25)
project(LibShared CXX)

include(mama.cmake)
include_directories(${MAMA_INCLUDES})

add_library(LibShared STATIC src/shared/api.cpp)
target_include_directories(LibShared PUBLIC ${CMAKE_CURRENT_SOURCE_DIR}/src)
target_link_libraries(LibShared ${MAMA_LIBS})

# The bottom of the diamond depends on nothing, so MAMA_MODULES is empty and mama_target_modules()
# returns before it defines MAMA_HAS_MODULES. Gate on the toolchain capability that mama.cmake set.
if(MAMA_MODULES_AVAILABLE)
    target_compile_features(LibShared PUBLIC cxx_std_20)
    target_sources(LibShared PUBLIC FILE_SET shared_modules TYPE CXX_MODULES
                   BASE_DIRS ${CMAKE_CURRENT_SOURCE_DIR}/src FILES src/shared/shared-api.cppm)
    install(TARGETS LibShared ARCHIVE DESTINATION lib FILE_SET shared_modules DESTINATION src)
else()
    install(TARGETS LibShared ARCHIVE DESTINATION lib)
endif()
