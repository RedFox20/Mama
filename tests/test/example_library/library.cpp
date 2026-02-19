#include "library.h"
#include <cstdio>
#include <filesystem>

namespace example
{
    // ensure C++17 features are used
    namespace fs = std::filesystem;

    bool print_file_exists(const std::string& str)
    {
        bool exists = fs::exists(str);
        #if BUILD_CONFIG
            printf("BUILD_CONFIG set; file_exists=%s\n", exists ? "yes" : "no");
        #else
            printf("BUILD_CONFIG unset; file_exists=%s\n", exists ? "yes" : "no");
        #endif
        return exists;
    }
}
