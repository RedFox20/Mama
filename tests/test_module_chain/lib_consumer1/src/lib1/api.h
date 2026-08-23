#pragma once
#include <rpp/strview.h>
#include <string>

namespace lib1
{
    /// The first whitespace token of `text`, through rpp::strview.
    std::string first_token(const char* text);
}
