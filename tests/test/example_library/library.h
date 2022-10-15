#pragma once
#include <string>

#ifndef DLLEXPORT
#  if _MSC_VER
#    define DLLEXPORT __declspec(dllexport)
#  else // clang/gcc
#    define DLLEXPORT __attribute__((visibility("default")))
#  endif
#endif

namespace example
{
    DLLEXPORT bool print_file_exists(const std::string& str);
}
