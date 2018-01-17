#include <cstdio>
#include <filesystem>

// ensure C++17 features are used
namespace fs = std::experimental::filesystem;

int main(int argc, char** argv)
{
    #if BUILD_CONFIG
        printf("BUILD_CONFIG set\n");
    #else
        printf("BUILD_CONFIG not set\n");
    #endif
    fs::exists(argv[0]);    
    return 0;
}
