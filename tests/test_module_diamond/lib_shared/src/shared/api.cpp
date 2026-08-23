#include "shared/api.h"

namespace shared
{
    std::string first_token(const char* text)
    {
        std::string all { text };
        size_t space = all.find(' ');
        return space == std::string::npos ? all : all.substr(0, space);
    }
}
