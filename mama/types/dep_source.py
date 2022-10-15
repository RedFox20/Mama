
class DepSource(object):
    def __init__(self, name):
        self.name = name
        self.is_git = False
        self.is_pkg = False
        self.is_src = False

    def get_papa_string(self):
        raise RuntimeError('get_papa_string() not implemented')


    @staticmethod
    def papa_join(*fields):
        """ Join all given fields for PAPA package serialization """
        strings = []
        for field in fields:
            if field:
                if isinstance(field, list):
                    strings += field
                else:
                    strings.append(field)
            else:
                strings.append('')
        return ",".join(strings)
