import pynvim
import requests
import warnings

class Location:
    def __init__(self, path, line_content, line_num):
        self.path = path
        self.content = line_content
        self.line_num = line_num

    def __str__(self):
        return '{}:{}\n  {}'.format(self.path, self.line_num, self.content.strip())

    def truncated_path(self, size=80):
        if len(self.path) < size:
            return self.path
        if size < 10:
            return self.path[:size]
        return self.path[:5] + '..' + self.path[-(size-5-2):]

    def truncated_str(self):
        return '{}:{}\n  {}'.format(self.truncated_path(), self.line_num, self.content.strip())

    def from_ogrok_dict(d):
        ret = []
        for path in d:
            lines = d[path]
            for line in lines:
                l  = ""
                ln = 0
                if line['line']:
                    l = line['line'] 
                if line['lineNumber']:
                    ln = line['lineNumber']
                ret.append(Location(path, l, ln))
        return ret

class Mark:
    def __init__(self, path, line_number):
        self.path = path
        self.line = line_number


class OpenGrokAPI:

    # addr is the location you'd go in a web browser
    # e.g., http://localhost:8080/source
    def __init__(self, addr, test=False):
        self.session = requests.Session()
        self.addr = '{}/api/v1/'.format(addr)
        if test:
            try:
                rsp = self.session.get(
                    self.addr + 'search?def=lkjsadadfkj&maxresults=1',
                    timeout=3, 
                )
                if not rsp.ok:
                    errmsg  = "OGrok: Host {} did not respond OK: {}"
                    raise Exception(errmsg.format(self.addr, rsp))
            except Exception as e:
                errmsg  = "OGrok: Failed to connect to {}: {}"
                raise Exception(errmsg.format(self.addr, e))




    def _search(self, key, s, count, fuzzy):
        if fuzzy:
            s = '*{}*'.format(s)
        get_all = False
        if count == -1:
            # get all, 1000 at a time
            get_all = True
            count = 1000
        reqfmt = self.addr + 'search?' + key + '={symbol}&maxresults={count}&start={idx}'
        req = reqfmt.format(symbol=s, count=count, idx=0)
        rsp = self.session.get(req, timeout=1)
        if not rsp.ok:
            raise Exception("Request '{}' failed ({}).".format(req,rsp))
        d = rsp.json()
        ret = d['results']
        if not get_all:
            return ret

        total = d['resultCount']
        times = 1
        while len(ret) < total:
            req = reqfmt.format(symbol=s, count=count, idx=len(ret))
            rsp = self.session.get(req)
            if not rsp.ok:
                raise Exception("Request '{}' failed ({}).".format(req,rsp))
            d = rsp.json()
            total = d['resultCount']
            ret.update(d['results'])

            times += 1
            if times > 10:
                warnings.warn("Server claims too many results. Returning early.")
                break
        return ret

    def search_symbol(self, s, count=-1, fuzzy=False):
        return self._search('symbol', s, count, fuzzy)

    def search_def(self, s, count=-1, fuzzy=False):
        return self._search('def', s, count, fuzzy)

    def search_path(self, s, count=-1, fuzzy=False):
        return self._search('path', s, count, fuzzy)
        



@pynvim.plugin
class OGrokPlugin(object):

    def __init__(self, nvim):
        self.nvim = nvim
        self.api = None
        self.path = None
        self.marks = []

        self.tmp_saved_locations = None
        # the buffer that the user was originally in before we made a new one
        self.tmp_work_buffer= None
        self.tmp_col = None
        self.tmp_row = None

    @pynvim.command('OGrokSetBasePath', nargs='*', range='', sync=True)
    def OGrokSetBasePath(self, args, range):
        # autocmd VimEnter * OGrokSetBasePath /home/user/src
        if len(args) < 1:
            raise Exception("Path argument required.")
        self.path = args[0]

    @pynvim.command('OGrokIsBasePathSet', nargs='0', range='', sync=True)
    def OGrokIsBasePathSet(self, args, range):
        if self.path:
            self.nvim.out_write('OpenGrok base path is {}\n'.format(self.path))
        else:
            self.nvim.out_write('OpenGrok base path is not set.\n')


    @pynvim.command('OGrokSetServer', nargs='*', range='', sync=True)
    def OGrokSetServer(self, args, range):
        # a command for the vimrc that will set this
        # autocmd VimEnter * OGrokSetServer http://example.com:8080/source 0
        host = args[0]
        test = False
        if len(args) > 1:
            test = args[1]
            if test == "1":
                test = True

        # we don't want them to give an entire backtrace if there's an exception
        raise_flag = False
        raise_val = None
        try:
            self.api = OpenGrokAPI(host, test)
        except Exception as e:
            raise_flag = True
            raise_val = 'OGrok: Failed to init: {}'.format(e)

        if raise_flag:
            raise Exception(raise_val)

    @pynvim.command('OGrokIsServerSet', nargs='0', range='', sync=True)
    def OGrokIsServerSet(self, args, range):
        if self.api:
            self.nvim.out_write('OpenGrok server is {}\n'.format(self.api.addr))
        else:
            self.nvim.out_write('OpenGrok server is not set.\n')

    @pynvim.command('OGrok', nargs='*', range='')
    def OGrok(self, args, range):

        self.tmp_saved_locations = None

        if self.api == None:
            raise Exception("Cannot query without a server. See OGrokSetServer.")

        if self.path == None:
            raise Exception("Cannot query without a base path. See OGrokSetBasePath.")

        if len(args) < 2:
            raise Exception("Usage: <def|file|sym> <query> [fuzzy_flag: 0|1]")

        query_type = args[0]
        query_value = args[1]

        fuzzy = False
        if len(args) == 3:
            fuzzy = "0" != args[2]

        if query_type in ['g', 'd', 'def',]:
            query_type = 0
        elif query_type in ['f', 'file', 'path']:
            query_type = 1
        elif query_type in ['s', 'sym']:
            query_type = 2
        else:
            raise Exception("Invalid query type. Options are def|file|sym")

        fns = [self.api.search_def, self.api.search_path, self.api.search_symbol]
        fn = fns[query_type]

        try:
            data = fn(query_value, -1, fuzzy)
        except Exception as e:
            self.nvim.err_write('OGrok: {}.\n'.format(e))
            return
        locations = Location.from_ogrok_dict(data)
        self.tmp_saved_locations = locations
        if len(locations) == 0:
            self.nvim.out_write('OGrok: No results.\n')
            return

        # save stuff off
        self.tmp_work_buffer = self.nvim.request('nvim_win_get_buf', 0)
        self.tmp_row, self.tmp_col = self.nvim.request('nvim_win_get_cursor', 0)

        # TODO if there's only one result, go there

        new_buf = self.nvim.request('nvim_create_buf', False, True)

        status = '~~ {} matches. ~~ [q to quit] ~~ [<return> to select] ~~'.format(len(locations))
        self.nvim.request('nvim_buf_set_lines', new_buf, 0, 1, True, [status])

        for i,l in enumerate(locations):
            new_buf.append('{idx} {path}:{line_num}'.format(idx=i,
                path=l.path, line_num=l.line_num))
            if query_type != 1:
                new_buf.append('  {content}'.format(content=l.content.strip()))

        closing_keys= ['<Esc>', '<Leader>', 'q']
        key_map_opts = {'silent': True, 'nowait': True, 'noremap': True}
        for key in closing_keys:
            self.nvim.request('nvim_buf_set_keymap', new_buf, 'n', key, ':close<CR>', key_map_opts)


        cmd = ':OGrokGoto<CR>'
        self.nvim.request('nvim_buf_set_keymap', new_buf, 'n', '<CR>', cmd, key_map_opts)
        

        # set this in vimrc
        # self.nvim.command("hi Pmenu ctermbg=blue guibg=blue")

        cur_win = self.nvim.request('nvim_get_current_win')
        options = {
            'relative': 'win',
            'width'   : cur_win.width,
            'height'  : cur_win.height//4,
            'row'     : cur_win.width*3//4,
            'col'     : 0,
            'anchor'  : 'NW',
            'style'   : 'minimal',
            'border'  : 'rounded',
        }
        new_win = self.nvim.request('nvim_open_win', new_buf, True, options)
        self.nvim.command("setlocal cursorline")
        self.nvim.command("setlocal nowrap")
        self.nvim.command("0")






    @pynvim.command('OGrokGoto', nargs='*', range='')
    def OGrokGoto(self, args, range):
        if None == self.tmp_saved_locations:
            s = "OGrokGoto shouldn't be called directly. "
            s += "If you didn't call directly and are seeing this error "
            s += "then something went wrong."
            raise Exception(s)

        row, col = self.nvim.request('nvim_win_get_cursor', 0)
        # 1-indexed # XXX Fixme, why is there a blank line at the start
        if row == 1:
            self.nvim.out_write('OGrok: please select a line.\n')
            return

        # get cur line and preceeding line
        # line is 0 indexed..... (but rows are 1 indexed)
        lines = self.nvim.request('nvim_buf_get_lines', 0, row-2, row, True)
        # get the line we want. If we searched for a file, both will have ints
        # at the beginning and we want the second. Otherwise, only one of the
        # two will have ints at the beginning.
        # so this loop either grabs the last line starting with an int (correct
        # in the file case) or the only line starting with an int (correct in
        # the other cases)
        x = None
        for line in lines:
            if len(line) == 0 or line[0] not in '1234567890':
                continue

            x = -1
            s = line.split()[0]
            try:
                x = int(s)
            except ValueError:
                self.nvim.out_write('OGrok: not int: {}.\n'.format(s))
                continue

        if None == x:
            self.nvim.out_write('OGrok: unable to handle selection.\n')
            return

        curr_fpath = self.nvim.request('nvim_buf_get_name', self.tmp_work_buffer)
        if len(curr_fpath) != 0:
            # if we have a location to save

            # save cur location
            self.marks.append(Mark(curr_fpath, self.tmp_row))

        # get next location
        loc = self.tmp_saved_locations[x]

        self.nvim.command(':close')

        cmd = ':e +{line} {base}{rest}'.format(
            base=self.path,
            rest=loc.path,
            line=loc.line_num
        )
        self.nvim.command(cmd)

        self.tmp_saved_locations = None
        self.tmp_work_buffer = None
        self.tmp_col = None
        self.tmp_row = None
        return
            

    @pynvim.command('OGrokJumpBack', nargs='0', range='')
    def OGrokJumpBack(self, args, range):
        if len(self.marks) == 0:
            self.nvim.out_write('OGrok: jump stack is empty.\n')
            return

        m = self.marks.pop()
        cmd = ':e +{line} {path}'.format(line=m.line, path=m.path)
        self.nvim.command(cmd)
