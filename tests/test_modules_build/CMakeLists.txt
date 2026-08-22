cmake_minimum_required(VERSION 3.25)
project(Consumer CXX)

include(mama.cmake)
include_directories(${MAMA_INCLUDES})

add_executable(Consumer ${CMAKE_CURRENT_SOURCE_DIR}/consumer.cpp)

if(WHOLE_ARCHIVE_LINK)
    # The link shape that finds two module initializers: the consumer compiles the module AND the
    # whole archive lands in the binary. It only builds when the package dropped its module object.
    target_link_options(Consumer PRIVATE "-Wl,--whole-archive" ${MAMA_LIBS} "-Wl,--no-whole-archive")
else()
    target_link_libraries(Consumer ${MAMA_LIBS})
endif()

mama_target_modules(Consumer)

install(TARGETS Consumer DESTINATION ${CMAKE_CURRENT_SOURCE_DIR}/bin)
