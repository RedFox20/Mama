// EVERY #include comes before the import: a header parsed after one re-declares what the module
// already made reachable, and GCC rejects that.
#include <string>
#include "libb/api.h"
#ifdef MAMA_HAS_MODULES
import shared.api;
#else
#  include "shared/api.h"
#endif

namespace libb
{
    std::string tag(const char* text) { return "b:" + shared::first_token(text); }
}
