#include "lib1/api.h"

namespace lib1
{
    std::string first_token(const char* text)
    {
        rpp::strview line { text };
        return line.next(' ').to_string();
    }
}
