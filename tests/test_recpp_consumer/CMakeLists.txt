cmake_minimum_required(VERSION 3.28)
project(consumer CXX)
include(${CMAKE_CURRENT_SOURCE_DIR}/mama.cmake)

add_executable(consumer main.cpp)
target_include_directories(consumer PRIVATE ${MAMA_INCLUDES})
mama_target_modules(consumer)
target_link_libraries(consumer PRIVATE ${MAMA_LIBS})
install(TARGETS consumer RUNTIME DESTINATION bin)
