import mama, sys, os

def main():
    if len(sys.argv) == 1:
        mama.print_usage()
        sys.exit(-1)
    config = mama.MamaBuildConfig(sys.argv[1:])
    test = mama.MamaBuildTarget('test', 'wolf3d', config=config)
    cwd = os.getcwd()
