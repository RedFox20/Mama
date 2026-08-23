// The top of a diamond. LibA and LibB each compile shared.api into their own archive, so the two
// sibling initializers collide with each other before this consumer's own copy enters the link.
#include <cstdio>
#include <string>
#include "liba/api.h"
#include "libb/api.h"
#ifdef MAMA_HAS_MODULES
import shared.api;
#else
#  include "shared/api.h"
#endif

int main()
{
    const char* text = "alpha beta gamma";
    if (liba::tag(text) != "a:alpha")          { std::printf("FAIL: liba gave the wrong tag\n"); return 1; }
    if (libb::tag(text) != "b:alpha")          { std::printf("FAIL: libb gave the wrong tag\n"); return 2; }
    if (shared::first_token(text) != "alpha")  { std::printf("FAIL: shared gave the wrong token\n"); return 3; }
#ifdef MAMA_HAS_MODULES
    std::printf("OK: MODULES\n");
#else
    std::printf("OK: HEADERS\n");
#endif
    return 0;
}
