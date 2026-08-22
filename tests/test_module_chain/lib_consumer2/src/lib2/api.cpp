#include "lib2/api.h"

namespace lib2
{
    std::string shout(const char* text)
    {
        std::string token = lib1::first_token(text);
        for (char& c : token) c = (char)std::toupper((unsigned char)c);
        return token;
    }
}
