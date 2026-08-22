// The top of a four level chain. Every level below exports a module, and this one imports all of
// them at once, so a duplicate initializer or a missing interface fails the build or the link.
#include <cstdio>
#include <string>  // an import names the type, and the comparison operators still come from here

#ifdef MAMA_HAS_MODULES
import lib2.api;
import lib1.api;
#else
#include "lib2/api.h"
#include "lib1/api.h"
#endif

int main()
{
    const char* text = "alpha beta gamma";
    if (lib1::first_token(text) != "alpha") { std::printf("FAIL: lib1 gave the wrong token\n"); return 1; }
    if (lib2::shout(text) != "ALPHA")       { std::printf("FAIL: lib2 gave the wrong token\n"); return 2; }
#ifdef MAMA_HAS_MODULES
    std::printf("OK: MODULES\n");
#else
    std::printf("OK: HEADERS\n");
#endif
    return 0;
}
