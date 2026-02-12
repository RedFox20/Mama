cmake_minimum_required(VERSION 3.20)
project(example_consumer)

include(mama.cmake)
include_directories(${MAMA_INCLUDES})

add_executable(ExampleConsumer consumer.cpp)
target_link_libraries(ExampleConsumer ${MAMA_LIBS})

install(TARGETS ExampleConsumer DESTINATION ${CMAKE_CURRENT_SOURCE_DIR}/bin)
