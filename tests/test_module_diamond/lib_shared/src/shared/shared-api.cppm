// The facade shape: the header stays the source of truth, and the module re-exports its names.
module;
#include "shared/api.h"

export module shared.api;

export namespace shared
{
    using shared::first_token;
}
