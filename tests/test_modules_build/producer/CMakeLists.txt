cmake_minimum_required(VERSION 3.28)
project(Producer CXX)

add_library(Producer STATIC src/rpp/strview.cpp)
target_include_directories(Producer PUBLIC ${CMAKE_CURRENT_SOURCE_DIR}/src)

# The producer builds the module too, so its own archive holds the module initializer object.
# The mama package drops that object, which is what the strip tests read.
if(BUILD_WITH_MODULES)
    target_compile_features(Producer PUBLIC cxx_std_20)
    target_sources(Producer PUBLIC FILE_SET producer_modules TYPE CXX_MODULES
                   BASE_DIRS ${CMAKE_CURRENT_SOURCE_DIR}/src FILES src/rpp/rpp-strview.cppm)
    install(TARGETS Producer ARCHIVE DESTINATION lib FILE_SET producer_modules DESTINATION src)
else()
    install(TARGETS Producer ARCHIVE DESTINATION lib)
endif()
