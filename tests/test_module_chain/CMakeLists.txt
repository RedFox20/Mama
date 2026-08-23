cmake_minimum_required(VERSION 3.25)
project(TopConsumer CXX)

include(mama.cmake)
include_directories(${MAMA_INCLUDES})

add_executable(TopConsumer ${CMAKE_CURRENT_SOURCE_DIR}/main.cpp)
if(UNIX AND NOT APPLE)
    # Whole-archive is the link shape that finds a duplicate module initializer, which a plain static
    # link tolerates. Only the .a entries go in: MAMA_LIBS also names system libs, taken as -l.
    set(CHAIN_ARCHIVES "")
    set(CHAIN_SYSLIBS "")
    foreach(lib ${MAMA_LIBS})
        if(lib MATCHES "\\.a$")
            list(APPEND CHAIN_ARCHIVES ${lib})
        else()
            list(APPEND CHAIN_SYSLIBS ${lib})
        endif()
    endforeach()
    target_link_libraries(TopConsumer ${CHAIN_SYSLIBS})
    target_link_options(TopConsumer PRIVATE "-Wl,--whole-archive" ${CHAIN_ARCHIVES} "-Wl,--no-whole-archive")
else()
    target_link_libraries(TopConsumer ${MAMA_LIBS})
endif()
mama_target_modules(TopConsumer)

install(TARGETS TopConsumer DESTINATION ${CMAKE_CURRENT_SOURCE_DIR}/bin)
