cmake_minimum_required(VERSION 3.25)
project(Consumer CXX)

include(mama.cmake)
include_directories(${MAMA_INCLUDES})

add_executable(Consumer ${CMAKE_CURRENT_SOURCE_DIR}/consumer.cpp)

target_link_libraries(Consumer ${MAMA_LIBS})

mama_target_modules(Consumer)

install(TARGETS Consumer DESTINATION ${CMAKE_CURRENT_SOURCE_DIR}/bin)
