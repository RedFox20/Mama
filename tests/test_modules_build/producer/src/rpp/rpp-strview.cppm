// The facade shape: the header stays the single source of truth, and the module only re-exports
// its names. An importer and an includer then name the same entity in one binary.
module;
#include "rpp/strview.h"

export module rpp.strview;

export namespace rpp
{
    using rpp::greet;
}
