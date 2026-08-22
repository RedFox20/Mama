// One check per mechanism a module facade can get wrong. The same source builds both ways, and a
// difference between the two reports is what this test looks for.

// EVERY #include comes before the import: a header parsed after one re-declares what the module
// already made reachable, and GCC 14 rejects that.
#include <rpp/config.h>
#include <cstdio>
#include <string>
#include <vector>
#ifdef MAMA_HAS_MODULES
import rpp.strview;
#  define BUILT_WITH "MODULES"
#else
#  include <rpp/strview.h>
#  define BUILT_WITH "HEADERS"
#endif

using namespace rpp::literals;

static int failed = 0;

static void check(const char* what, bool ok)
{
    printf("  %-28s %s\n", what, ok ? "ok" : "FAIL");
    if (!ok) ++failed;
}

int main()
{
    printf("consumer built with %s\n", BUILT_WITH);

    { // the core use: the re-exported type parses a real string
        std::vector<std::string> tokens;
        rpp::strview line{"alpha,,gamma"}, token;
        while (line.next(token, ',')) tokens.push_back(std::string{token.str, (size_t)token.len});
        check("strview::next", tokens == std::vector<std::string>{"alpha", "", "gamma"});
    }
    check("_sv literal",     rpp::strview{"abc"} == "abc"_sv);
    check("operator==",      rpp::strview{"abc"} == "abc");
    check("operator+",       std::string{rpp::strview{"ab"} + rpp::strview{"cd"}} == "abcd");
    check("free function",   rpp::to_int("42") == 42);
    check("member function", rpp::strview{"1.5"}.to_float() == 1.5f);
    { rpp::string s = "ABC"; check("rpp::string", rpp::to_lower(s) == "abc"); }
    { // a re-exported class carries its own state across calls
        rpp::line_parser parser{"first\nsecond\nthird"};
        rpp::strview line; int lines = 0;
        while (parser.read_line(line)) ++lines;
        check("line_parser", lines == 3);
    }
    check("template", sizeof(rpp::strview_traits<rpp::strview>::strview_t) == sizeof(rpp::strview));
    check("concept",  rpp::StringViewType<rpp::strview>);

#if RPP_ENABLE_UNICODE
    { // the facade re-exports these behind the same #if, and no module carries the macro
        rpp::ustrview u{u"abc"};
        check("ustrview", u.len == 3 && rpp::to_string(u) == "abc");
    }
#endif

    if (failed) { printf("FAIL: %d check(s)\n", failed); return 1; }
    printf("OK: every check passed\n");
    return 0;
}
