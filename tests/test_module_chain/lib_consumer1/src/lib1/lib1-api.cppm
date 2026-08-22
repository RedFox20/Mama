// The facade shape: the header stays the source of truth, and the module re-exports its names.
module;
#include "lib1/api.h"

export module lib1.api;

export namespace lib1
{
    using lib1::first_token;
}
