cmake_minimum_required(VERSION 3.25)
project(TopConsumer CXX)

include(mama.cmake)
include_directories(${MAMA_INCLUDES})

add_executable(TopConsumer ${CMAKE_CURRENT_SOURCE_DIR}/main.cpp)
if(UNIX AND NOT APPLE)
    # The link shape that finds a duplicate module initializer. A plain static link pulls a member only
    # to resolve a symbol, so it tolerates the copy this diamond must not carry. Only the archives go in:
    # MAMA_LIBS also names system libs, which the linker takes as -l and not as a path.
    set(D_ARCHIVES "")
    set(D_SYSLIBS "")
    foreach(lib ${MAMA_LIBS})
        if(lib MATCHES "\\.a$")
            list(APPEND D_ARCHIVES ${lib})
        else()
            list(APPEND D_SYSLIBS ${lib})
        endif()
    endforeach()
    target_link_libraries(TopConsumer ${D_SYSLIBS})
    target_link_options(TopConsumer PRIVATE "-Wl,--whole-archive" ${D_ARCHIVES} "-Wl,--no-whole-archive")
else()
    target_link_libraries(TopConsumer ${MAMA_LIBS})
endif()
mama_target_modules(TopConsumer)

install(TARGETS TopConsumer DESTINATION ${CMAKE_CURRENT_SOURCE_DIR}/bin)
