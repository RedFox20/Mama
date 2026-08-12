// One source, both paths. mama_target_modules() defines MAMA_HAS_MODULES when the toolchain
// carries modules, so the printed line names which path the build took.
#include <cstdio>

#ifdef MAMA_HAS_MODULES
import rpp.strview;
#else
#include "rpp/strview.h"
#endif

int main()
{
#ifdef MAMA_HAS_MODULES
    std::printf("MODULES %s\n", rpp::greet().c_str());
#else
    std::printf("HEADERS %s\n", rpp::greet().c_str());
#endif
    return 0;
}
