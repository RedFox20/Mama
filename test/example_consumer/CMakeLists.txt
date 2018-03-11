cmake_minimum_required(VERSION 3.6)
project(example_consumer)

include(mama.cmake)
include_directories(${MAMA_INCLUDES})

file(GLOB_RECURSE EXAMPLE_CONSUMER_SOURCES *.cpp *.h)
source_group(ExampleConsumer FILES ${EXAMPLE_CONSUMER_SOURCES})
add_executable(ExampleConsumer ${EXAMPLE_CONSUMER_SOURCES})
target_link_libraries(ExampleConsumer ${MAMA_LIBS})

install(TARGETS ExampleConsumer DESTINATION ${CMAKE_CURRENT_SOURCE_DIR}/bin)
