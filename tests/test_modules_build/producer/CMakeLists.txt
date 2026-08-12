cmake_minimum_required(VERSION 3.28)
project(Producer CXX)

add_library(Producer STATIC src/rpp/strview.cpp)
target_include_directories(Producer PUBLIC ${CMAKE_CURRENT_SOURCE_DIR}/src)

# This archive keeps the module initializer object, and the mama package drops it. No standard is named
# here on purpose, so a CXX_MODULES file set proves that mama alone told cmake the standard.
if(BUILD_WITH_MODULES)
    target_sources(Producer PUBLIC FILE_SET producer_modules TYPE CXX_MODULES
                   BASE_DIRS ${CMAKE_CURRENT_SOURCE_DIR}/src FILES src/rpp/rpp-strview.cppm)
    install(TARGETS Producer ARCHIVE DESTINATION lib FILE_SET producer_modules DESTINATION src)
else()
    install(TARGETS Producer ARCHIVE DESTINATION lib)
endif()
