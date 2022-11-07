import pynvim
import requests
import warnings
import os
import sqlite3

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
    def __init__(self, path, line_number, col):
        self.path = path
        self.line = line_number
        self.col  = col

    def __str__(self):
        path = self.path
        # XXX windows vs linux
        # just os.path...
        if '/' in self.path:
            path = self.path.split('/')[-1]
        elif '\\' in self.path:
            path = self.path.split('\\')[-1]

        if len(path) > 15:
            path = path[-15:]

        return f'Mark({path}:{self.line}|{self.col})'


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




    # TODO URL Encode stuff. stuff can have non-url path stuff in it? Or does requests handle that already..?
    def _search(self, key, s, count, fuzzy, proj_name):
        if fuzzy:
            s = '*{}*'.format(s)
        get_all = False
        if count == -1:
            # get all, 1000 at a time
            get_all = True
            count = 1000
        req = ""
        if proj_name:
            reqfmt = self.addr + 'search?' + key + '={symbol}&maxresults={count}&start={idx}&projects={proj}'
            req = reqfmt.format(symbol=s, count=count, idx=0, proj=proj_name)
        else:
            reqfmt = self.addr + 'search?' + key + '={symbol}&maxresults={count}&start={idx}'
            req = reqfmt.format(symbol=s, count=count, idx=0)

        rsp = self.session.get(req, timeout=5)
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
                # TODO replace this with normal error handling...
                # Maybe make the pop up window just say "partial results" or something
                warnings.warn("Server claims too many results. Returning early.")
                break
        return ret

    def search_symbol(self, s, count=-1, fuzzy=False, proj_name=None):
        return self._search('symbol', s, count, fuzzy, proj_name)

    def search_def(self, s, count=-1, fuzzy=False, proj_name=None):
        return self._search('def', s, count, fuzzy, proj_name)

    def search_path(self, s, count=-1, fuzzy=False, proj_name=None):
        return self._search('path', s, count, fuzzy, proj_name)

import threading
import time
class KeepaliveThread(threading.Thread):
    def __init__(self, keepalive, api):
        super().__init__()
        self.shutdown_flag = False
        self.keepalive = keepalive
        self.api = api
        self.count = 0

    def run(self):
        def callback():
            self.count += 1
            self.api.session.get(
                self.api.addr + 'search?def=lkjsadadfkj&maxresults=1',
                timeout=3,
            )
        while not self.shutdown_flag:
            timer = threading.Timer(self.keepalive, callback)
            timer.start()
            timer.join()





@pynvim.plugin
class OGrokPlugin(object):

    def __init__(self, nvim):
        self.nvim = nvim
        self.api = None
        self.path = None
        # map from window id to the array of Marks
        self.marks = {}
        self.log = None

        # sqlite3 database on disk
        self.annotations_db = None
        # dict for signs that are active so we can quickly get the annotation
        # and clean up later
        #    (fname, line) -> (note, tags, id)
        self.annotation_ids = dict()
        # randomish starting point
        self.annotation_counter = 0xff00

        # map tags to sign styles
        self.signstyle_tag_map = {}


        # "ping" the open grok server so so that the requests session doesn't die.
        # When the session dies, there's a noticable lag in getting the server resp
        self.keepalive_thread = None

        self.tmp_saved_locations = None
        # the buffer that the user was originally in before we made a new one
        self.tmp_work_buffer= None
        self.tmp_work_window = None
        self.tmp_col = None
        self.tmp_row = None


    def normalize_path(self, path):
        # idk... there's a problem where the call commands :blah <path>
        # where things go bad if there are backslashes in the path.
        try:
            return os.path.realpath(path).replace('\\', '/')
        except:
            # XXX specifically do file not found? What other exceptions can we
            # get?
            return None

    def setup_signs(self):
        if self.annotations_db == None:
            raise("OGrok: annotation database file must be set")

        conn = None
        try:
            conn = sqlite3.connect(self.annotations_db)
            cur = conn.cursor()
            tables = cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='AnnotationTable'").fetchall()
            if len(tables) == 0:
                cur.execute("CREATE TABLE AnnotationTable(file, line, annotation, tags)")

        except Exception as e:
            self.nvim.err_write(f"OGrok: Failed SQL operation during setup: {e}")
            raise(e)
        finally:
            if conn:
                conn.close()


        # setup default sign style
        try:
            # apparently this throws when it isn't found
            data = self.nvim.command(':sign list OGrokAnnotationSign')
        except pynvim.api.common.NvimError as e:
            if "E155" in str(e):
                # add the sign (max 2 chars)
                s = '>>'
                #s = '⚞'
                #s = '☰'
                # TODO make the char and hilight configurable
                # texthl=Todo, texthl=Error
                def_sign = f'sign define OGrokAnnotationSign text={s} texthl=Error'
                self.nvim.command(def_sign)
            else:
                raise(e)




    @pynvim.command('OGrokNextAnnotation', nargs='0', range='', sync=True)
    def OGrokNextAnnotation(self, args, range):
        # show notes for the current file
        # autocmd BufEnter * OGrokTryGetNotesForFile

        if None == self.annotations_db:
            self.nvim.out_write(f'OGrok: annotations database path must be set.\n')
            return

        data = self.__OGrokTryGetNotesForFile()
        if None == data or len(data) == 0:
            self.nvim.out_write(f'OGrok: No annotations for this file.\n')
            return

        fname = data[0][0]

        buf = self.nvim.request('nvim_get_current_buf')
        win = self.nvim.request('nvim_get_current_win')
        row, col = self.nvim.request('nvim_win_get_cursor', 0)
        curr_fpath = self.nvim.request('nvim_buf_get_name', 0)

        if len(curr_fpath) != 0:
            # if we have a location to save

            # save cur location in the tag stack (for the given window)
            if win in self.marks:
                self.marks[win].append(Mark(curr_fpath, row, col))
            else:
                self.marks[win] = [Mark(curr_fpath, row, col)]

        # get next location
        new_line_num = None
        min_dist = None
        for f, l, note, tag in data:
            dist = l - row
            # <= because we want the current location to be the last place
            # we go back to.
            if dist <= 0:
                # XXX this should just be the length of the file.
                # too lazy to get file len right now.
                # all ops should wrap around the end of the file
                dist += 100000000000

            if None == new_line_num or dist < min_dist:
                new_line_num = l
                min_dist = dist
            
            

        # move that buffer to the location we want
        cmd = f':{new_line_num}'
        self.nvim.command(cmd)

        #cmd = f':OGrokDoAnnotation'
        #self.nvim.command(cmd)

        return







    @pynvim.command('OGrokShowAnnotations', nargs='0', range='', sync=True)
    def OGrokShowAnnotations(self, args, range):
        # show notes for the current file
        # autocmd BufEnter * OGrokTryGetNotesForFile

        if None == self.annotations_db:
            self.nvim.out_write(f'OGrok: annotations database path must be set.\n')
            return

        data = self.__OGrokTryGetNotesForFile()
        if None == data or len(data) == 0:
            self.nvim.out_write(f'OGrok: No annotations for this file.\n')
            return

        fname = data[0][0]

        # XXX duplicated a bunch of this. Make a function for it
        self.tmp_work_buffer = self.nvim.request('nvim_get_current_buf')
        self.tmp_work_window = self.nvim.request('nvim_get_current_win')
        self.tmp_row, self.tmp_col = self.nvim.request('nvim_win_get_cursor', 0)

        line = self.tmp_row


        # created a buf... need to clean up on err
        new_buf = self.nvim.request('nvim_create_buf', False, True)
        try:

            status = f'------ [READ ONLY] NOTES FOR {fname} ------'

            self.nvim.request('nvim_buf_set_lines', new_buf, 0, 1, True, [status])

            for f, l, note in data:
                new_buf.append(f"~~ line : {l: 4d} " + "~"*30)
                # removes repeated new lines?
                # -- nope. seems to work
                for l in note.split("\n"):
                    new_buf.append(l)
                #new_buf.append("")

            # XXX kinda want a warning before closing with unsaved changes...
            closing_keys= ['q']
            key_map_opts = {'silent': True, 'nowait': True, 'noremap': True}
            # close this window+buffer. Go back to correct window
            close_cmd = ':close | '
            close_cmd += 'call nvim_set_current_win({})<CR>'.format(self.tmp_work_window.handle)
            for key in closing_keys:
                self.nvim.request('nvim_buf_set_keymap', new_buf,
                        'n', key, close_cmd, key_map_opts)



            cur_win = self.nvim.request('nvim_get_current_win')
            height  = self.nvim.request('nvim_win_get_height', cur_win)
            width   = self.nvim.request('nvim_win_get_width',  cur_win)
            ht = height//4
            options = {
                'relative': 'win',
                'width'   : width,
                'height'  : ht,
                'row'     : height-ht,
                'col'     : 0,
                'anchor'  : 'NW',
                'style'   : 'minimal',
                'border'  : 'rounded',
            }
            new_win = self.nvim.request('nvim_open_win', new_buf, True, options)
            # XXX Set cursor position to second row

        except Exception as e:
            self.nvim.command(":close")
            raise e



    @pynvim.command('OGrokTryGetNotesForFile', nargs='0', range='', sync=True)
    def OGrokTryGetNotesForFile(self, args, range):
        # autocmd BufEnter * OGrokTryGetNotesForFile

        if None == self.annotations_db:
            self.nvim.out_write(f'OGrok: annotations database path must be set.\n')
            return

        data = self.__OGrokTryGetNotesForFile()
        self.__OGrokTrySetNotesForFile(data)



    # XXX so we can call it ourselves. Probabl don't need to actually do this this way
    def __OGrokTryGetNotesForFile(self):
        # vim cares about the bufname
        bufname = self.nvim.request('nvim_buf_get_name', 0)

        if len(bufname) == 0:
            # not in a file, nothing to annotate
            #self.nvim.out_write(f'OGrok: not in a file.\n')
            return

        # we use the normalized name in our db
        fname = self.normalize_path(bufname)
        if None == fname:
            return


        conn = None
        data = []
        try:
            conn = sqlite3.connect(self.annotations_db)
            cur = conn.cursor()
            data = cur.execute("SELECT file, line, annotation, tags from AnnotationTable WHERE file=?", (fname,)).fetchall()
        except Exception as e:
            self.nvim.err_write(f"OGrok: Failed SQL operation: {e}\n")
            raise(e)
        finally:
            if conn:
                conn.close()

        return data


    # data is a list returned from a sql statment
    def __OGrokTrySetNotesForFile(self, data):
        # vim cares about the bufname
        bufname = self.nvim.request('nvim_buf_get_name', 0)

        if len(bufname) == 0:
            # not in a file, nothing to annotate
            #self.nvim.out_write(f'OGrok: not in a file.\n')
            return

        # we use the normalized name in our db
        fname = self.normalize_path(bufname)
        if None == fname:
            return

        if len(data) == 0:
            #self.nvim.out_write(f'OGrok: no annotations for {fname}.\n')
            return

        for f, l, note, tags in data:
            if f != fname:
                raise Exception(f'{f} != {fname}')
            if (f,l) not in self.annotation_ids.keys():
                cmd = f':sign place {self.annotation_counter} name=OGrokAnnotationSign line={l} file={bufname}'
                self.nvim.command(cmd)
                self.annotation_ids[(f,l)]  = (note, tags, self.annotation_counter)
                self.annotation_counter += 1
            else:
                oldnote, oldtags, idd = self.annotation_ids[(f,l)]
                self.annotation_ids[(f,l)] = (note, tags, idd)









    @pynvim.command('OGrokDoAnnotation', nargs='0', range='', sync=True)
    def OGrokDoAnnotation(self, args, range):
        bufname = self.nvim.request('nvim_buf_get_name', 0)
        if len(bufname) == 0:
            # not in a file, nothing to annotate
            self.nvim.out_write(f"OGrok: Can't make annotation, not in a file.\n")
            return

        fname = self.normalize_path(bufname)
        if None == fname:
            self.nvim.out_write(f"OGrok: Can't make annotation, file not on filesystem.\n")
            return


        # we're going to read from our dict. need to make sure it's setup first.
        if len(self.annotation_ids) == 0:
            data = self.__OGrokTryGetNotesForFile()
            self.__OGrokTrySetNotesForFile(data)


        # XXX duplicated a bunch of this. Make a function for it
        self.tmp_work_buffer = self.nvim.request('nvim_get_current_buf')
        self.tmp_work_window = self.nvim.request('nvim_get_current_win')
        self.tmp_row, self.tmp_col = self.nvim.request('nvim_win_get_cursor', 0)

        line = self.tmp_row

        note, sign_id = '', -1
        tags = ''
        new = 'NEW'
        if (fname, line) in self.annotation_ids.keys():
            note, tags, sign_id = self.annotation_ids[(fname, line)]
            new = 'EDIT'

        # created a buf... need to clean up on err
        new_buf = self.nvim.request('nvim_create_buf', False, True)
        try:

            status  = f'~~ [{new} note] ~~ {fname}:{line} ~~'
            tagline = f'~~ TAGS: {tags}'

            self.nvim.request('nvim_buf_set_lines', new_buf, 0, 1, True, [status, tagline])

            if sign_id != -1:
                # removes repeated new lines?
                # -- nope. seems to work
                for l in note.split("\n"):
                    new_buf.append(l)

            # XXX kinda want a warning before closing with unsaved changes...
            closing_keys= ['q']
            key_map_opts = {'silent': True, 'nowait': True, 'noremap': True}
            # close this window+buffer. Go back to correct window
            close_cmd = ':close | '
            close_cmd += 'call nvim_set_current_win({})<CR>'.format(self.tmp_work_window.handle)
            for key in closing_keys:
                self.nvim.request('nvim_buf_set_keymap', new_buf,
                        'n', key, close_cmd, key_map_opts)


            cur_win = self.nvim.request('nvim_get_current_win')
            height  = self.nvim.request('nvim_win_get_height', cur_win)
            width   = self.nvim.request('nvim_win_get_width',  cur_win)
            ht = height//4
            options = {
                'relative': 'win',
                'width'   : width,
                'height'  : ht,
                'row'     : height-ht,
                'col'     : 0,
                'anchor'  : 'NW',
                'style'   : 'minimal',
                'border'  : 'rounded',
            }
            new_win = self.nvim.request('nvim_open_win', new_buf, True, options)
            # XXX Set cursor position to second row


            # make it so :w will send to db instead of disk
            # XXX other commands to alias here? :x?
            # :cabbrev <buffer> w MyCommand
            # XXX CAN'T HANDLE SPACES IN FILE NAMES???
            if ' ' in fname or "'" in fname or '"' in fname:
                raise Exception("Unimplemented: Can't handle space or quotes in path :(")
            if ' ' in bufname or "'" in bufname or '"' in bufname:
                raise Exception("Unimplemented: Can't handle space or quotes in path :(")
            #cmd = f'cabbrev <buffer> w OGrokAddNote {fname} {line}'
            # see vim.fandom.com/wiki/Replace_a_builtin_command_using_cabbrev
            # tldr, you can get into trouble if the w appears somewhere not
            # at the beginning
            cmd = f"cabbrev <buffer> w <c-r>=(getcmdtype()==':' && getcmdpos()==1 ? 'OGrokAddNote {fname} {bufname} {line}' : 'w')<CR>"
            self.nvim.command(cmd)
            # XXX make it so the OGrokAddNote thing doesn't appear in history


        except Exception as e:
            self.nvim.command(":close")
            raise e


    # XXX should be static
    def _tagname2signname(self, tag):
        return f'OGokAnnotationTag{tag}'


    @pynvim.command('OGrokAddTagStyle', nargs='*', range='', sync=True)
    def OGrokAddTagStyle(self, args, range):
        # autocmd VimEnter * OGrokAddTagStyle chars hlgroup
        if len(args) < 3:
            raise Exception("Provide tagname, chars, and highlight. E.g., OGrokAddTagStyle BUG >> Error")
        if len(args[1]) > 2:
            raise(Exception("Tag mark too long. Must be <= two characters"))

        tag = args[0].lower()
        self.signstyle_tag_map[tag] = (args[1], args[2])

        sign = self._tagname2signname(tag)
        try:
            # apparently this throws when it isn't found
            data = self.nvim.command(f':sign list {sign}')
        except pynvim.api.common.NvimError as e:
            if "E155" in str(e):
                # add the sign (max 2 chars)
                s = args[1]
                h = args[2]
                def_sign = f'sign define {sign} text={s} texthl={h}'
                self.nvim.command(def_sign)
            else:
                raise(e)



    # args: fname bufname line
    # use current buffer as "note" for given file and line.
    # Delete mark if result note is empty
    # not to be called directly
    @pynvim.command('OGrokAddNote', nargs='*', range='', sync=True)
    def OGrokAddNote(self, args, range):
        if len(args) != 3:
            self.nvim.out_write(f"OGrok: provide filename and line.\n")
            return

        fname = args[0]
        bufname = args[1]
        lineno = -1
        try:
            lineno = int(args[2])
        except Exception as e:
            self.nvim.out_write(f"OGrok: line must be an integer: {e}.\n")
            return


        # get the data
        # get cur line and preceeding line
        # line is 0 indexed..... (but rows are 1 indexed)
        # range [0, -1) should give us everything
        lines = self.nvim.request('nvim_buf_get_lines', 0, 0, -1, False)

        content  = []
        comments = []
        for line in lines:
            if line.startswith('~~'):
                comments.append(line[2:])
            else:
                content.append(line)

        taglist = []
        magic = 'tags:'
        for comment in comments:
            # comments dont' have the leading ~~ right now
            tmp = comment.strip().lower()
            if tmp.startswith(magic):
                idx = tmp.find(magic)
                tmp = tmp[idx + len(magic):]
                t = tmp.strip().split(",")
                for tag in t:
                    taglist.append(tag.strip())


        note = '\n'.join(content)
        tags = ', '.join(taglist)

        # default
        signname = 'OGrokAnnotationSign' 
        for tag in taglist:
            if tag in self.signstyle_tag_map.keys():
                signname = self._tagname2signname(tag)


        conn = None
        try:
            conn = sqlite3.connect(self.annotations_db)
            cur = conn.cursor()

            existing = cur.execute('SELECT file, line FROM AnnotationTable WHERE file=? AND line=?', (fname, lineno)).fetchall()
            if len(existing) > 1:
                # this shouldn't happen
                assert(False)
                pass
            elif len(existing) == 0:
                if len(note) > 0 or len(tags) > 0:
                    cur.execute("INSERT INTO AnnotationTable (file, line, annotation, tags) VALUES (?, ?, ?, ?)", (fname, lineno, note, tags))
                    conn.commit()
                else:
                    # nothing in db and our annotation is empty. Nothing to do
                    pass
            else:
                if len(note) > 0 or len(tags) > 0:
                    # something already there and we have a note. Update existing
                    cur.execute("UPDATE AnnotationTable SET annotation=?, tags=? WHERE file=? AND line=?", (note, tags, fname, lineno))
                    conn.commit()
                    # XXX do we need to commit after UPDATE?
                else:
                    # something there and we have an empty note. Delete existing
                    cur.execute("DELETE FROM AnnotationTable WHERE file=? AND line=?", (fname, lineno))
                    conn.commit()

        except Exception as e:
            self.nvim.err_write(f"OGrok: Failed SQL operation: {e}\n")
            raise(e)
        finally:
            if conn:
                conn.close()

        already = (fname, lineno) in self.annotation_ids.keys()
        if (len(note) > 0 or len(tags) > 0):
            # add mark, remove old one if necessary
            cmd = f':sign place {self.annotation_counter} name={signname} line={lineno} file={bufname}'
            self.nvim.command(cmd)
            if already:
                _, _, idd = self.annotation_ids[(fname, lineno)]
                cmd = f':sign unplace {idd} file={bufname}'
                self.nvim.command(cmd)
                self.annotation_ids.pop((fname,lineno))
            self.annotation_ids[(fname,lineno)] = (note, tags, self.annotation_counter)
            self.annotation_counter += 1
        else:
            _, _, idd = self.annotation_ids[(fname, lineno)]
            cmd = f':sign unplace {idd} file={bufname}'
            self.nvim.command(cmd)
            self.annotation_ids.pop((fname,lineno))
        #elif len(note) == 0  and len(tags) == 0 and already:
            #_, _, idd = self.annotation_ids[(fname, lineno)]
            #cmd = f':sign unplace {idd} file={bufname}'
            #self.nvim.command(cmd)
            #self.annotation_ids.pop((fname,lineno))


    @pynvim.command('OGrokDumpAnnotationShadow', nargs='0', range='')
    def OGrokDumpAnnotationShadow(self, args, range):
        self.nvim.out_write('OGrok: {}.\n'.format(self.annotation_ids))
        return


    @pynvim.command('OGrokSetAnnotationPath', nargs='*', range='', sync=True)
    def OGrokSetAnnotationPath(self, args, range):
        # autocmd VimEnter * AnnotationPath /path/to/file
        if len(args) < 1:
            raise Exception("Path argument required.")
        self.annotations_db = args[0]
        self.setup_signs()



    @pynvim.command('OGrokGetAnnotationPath', nargs='0', range='', sync=True)
    def OGrokGetAnnotationPath(self, args, range):
        if self.annotations_db:
            self.nvim.out_write(f'OGrok: Annotation database {self.annotations_db}.\n')
        else:
            self.nvim.out_write('OGrok: No annotation database.\n')


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

    @pynvim.command('OGrokSetKeepalive', nargs='*', range='', sync=True)
    def OGrokSetKeepalive(self, args, range):
        # args is time in seconds. 0 means off
        if len(args) < 1:
            raise Exception("Time in seconds required, 0 to disable.")
        t = 0
        try:
            t = int(args[0])
        except Exception as e:
            raise_val = 'OGrok: Failed to set keepalive: {}'.format(e)
            return

        if 0 == t:
            # shutdown if we're running a keepalive thread
            if self.keepalive_thread:
                self.keepalive_thread.keepalive = 0
                self.keepalive_thread.shutdown_flag = True
                self.keepalive_thread.join()
                self.keepalive_thread = None
                return
            else:
                self.keepalive_thread = None
                return
        else:
            if self.keepalive_thread:
                # if we have a keepalive thread, change the time
                self.keepalive_thread.keepalive = t
                return
            else:
                # otherwise start one if we have an api already
                if not self.api:
                    self.nvim.out_write('OGrok: error setting keepalive, server must be set first.\n')
                    return
                self.keepalive_thread = KeepaliveThread(t, self.api)
                self.keepalive_thread.start()
                return

    @pynvim.command('OGrokGetKeepalive', nargs='0', range='', sync=True)
    def OGrokGetKeepalive(self, args, range):
        if self.keepalive_thread:
            k = self.keepalive_thread.keepalive
            count = self.keepalive_thread.count
            self.nvim.out_write(f'OGrok: keepalive every {k} seconds ({count} keepalives sent).\n')
        else:
            self.nvim.out_write('OGrok: No keepalive thread.\n')




    @pynvim.command('OGrokSetLogFile', nargs='*', range='', sync=True)
    def OGrokSetLogFile(self, args, range):
        # autocmd VimEnter * OGrokSetBasePath /home/user/src
        if len(args) < 1:
            raise Exception("Path argument required.")
        self.log = args[0]

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

    @pynvim.command('OGrokGetCurrentProj', nargs='0', range='', sync=True)
    def OGrokGetCurrentProj(self, args, range):
        proj = self.get_current_project()
        if proj:
            self.nvim.out_write("OGrok: current project is '{}'.\n".format(proj))
        else:
            self.nvim.out_write('OGrok: no current project.\n')


    def get_current_project(self):
        cwd = self.nvim.command_output("echo getcwd()")
        cwd  = os.path.realpath(cwd)

        for proj in os.listdir(self.path):
            projpath = os.path.realpath(self.path)
            projpath = os.path.join(projpath, proj)

            common = os.path.commonpath([projpath, cwd])
            head, tail = os.path.split(common)
            if tail == proj and head == os.path.realpath(self.path):
                return proj

        return None

    # TODO document the API here....
    @pynvim.command('OGrok', nargs='*', range='', sync=True)
    def OGrok(self, args, range):

        self.tmp_saved_locations = None

        if self.api == None:
            self.nvim.err_write('OGrok: Cannot query without a server. See OGrokSetServer.\n')
            return

        if self.path == None:
            self.nvim.err_write('OGrok: Cannot query without a base path. See OGrokSetBasePath.\n')
            return

        if len(args) < 2:
            self.nvim.err_write('OGrok: Usage: <def|file|sym> <query> [fuzzy_flag: 0|1] [cur_proj_flag: 0|1]\n')
            return

        query_type = args[0]
        query_value = args[1]

        fuzzy = False
        if len(args) >= 3:
            fuzzy = "0" != args[2]

        proj_name = None
        if len(args) == 4:
            if "0" != args[3]:
                proj_name = self.get_current_project()



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
            data = fn(query_value, -1, fuzzy, proj_name)
        except Exception as e:
            self.nvim.err_write('OGrok: {}.\n'.format(e))
            return
        locations = Location.from_ogrok_dict(data)
        self.tmp_saved_locations = locations
        if len(locations) == 0:
            # TODO hitting this makes you go back to the beginning of the line
            # you're on??
            self.nvim.out_write('OGrok: No results.\n')
            return

        if self.log:
            with open(self.log, 'a+') as f:
                f.write("Data: {}".format(data))

        # XXX make a function that does this stuff...
        # save stuff off
        self.tmp_work_buffer = self.nvim.request('nvim_get_current_buf')
        self.tmp_work_window = self.nvim.request('nvim_get_current_win')
        self.tmp_row, self.tmp_col = self.nvim.request('nvim_win_get_cursor', 0)

        # TODO if there's only one result, go there

        # created a buf... need to clean up on err
        new_buf = self.nvim.request('nvim_create_buf', False, True)
        try:

            status = '~~ {} matches. ~~ [q to quit] ~~ [<return> to select] ~~'.format(len(locations))
            self.nvim.request('nvim_buf_set_lines', new_buf, 0, 1, True, [status])

            for i,l in enumerate(locations):
                if query_type != 1:
                    new_buf.append('{idx} {path}:{line_num}'.format(idx=i,
                        path=l.path, line_num=l.line_num))
                    # XXX do this properly
                    content = l.content.strip().replace('<b>', "")\
                            .replace('</b>', "")\
                            .replace('\n', 'XXXX')\
                            .replace('\r', 'YYYY')\
                            .replace("&gt;", ">")\
                            .replace("&lt;", "<")\
                            .replace("&amp;", "&")
                    new_buf.append('        {content}'.format(content=content))
                    new_buf.append('')
                else:
                    new_buf.append('{idx} {path}'.format(idx=i, path=l.path))

            closing_keys= ['<Esc>', '<Leader>', 'q', '<BS>']
            key_map_opts = {'silent': True, 'nowait': True, 'noremap': True}
            # close this window+buffer. Go back to correct window
            close_cmd = ':close | '
            close_cmd += 'call nvim_set_current_win({})<CR>'.format(self.tmp_work_window.handle)
            for key in closing_keys:
                self.nvim.request('nvim_buf_set_keymap', new_buf,
                        'n', key, close_cmd, key_map_opts)


            cmd = ':OGrokGoto<CR>'
            self.nvim.request('nvim_buf_set_keymap', new_buf, 'n', '<CR>', cmd, key_map_opts)

            # set this in vimrc
            # self.nvim.command("hi Pmenu ctermbg=blue guibg=blue")

            cur_win = self.nvim.request('nvim_get_current_win')
            height  = self.nvim.request('nvim_win_get_height', cur_win)
            width   = self.nvim.request('nvim_win_get_width',  cur_win)
            ht = height//4
            options = {
                'relative': 'win',
                'width'   : width,
                'height'  : ht,
                'row'     : height-ht,
                'col'     : 0,
                'anchor'  : 'NW',
                'style'   : 'minimal',
                'border'  : 'rounded',
            }
            new_win = self.nvim.request('nvim_open_win', new_buf, True, options)
            self.nvim.command("setlocal cursorline")
            self.nvim.command("setlocal nowrap")
            self.nvim.command("0")
            # TODO, get the \< \> to work...
            to_match = '\\<{}\\>'.format(query_value)
            if fuzzy:
                to_match = '\\<\\w*{}\\w*\\>'.format(query_value)
            self.nvim.command(":match Function /{}/".format(to_match))
            # idk, + and \+ don't seem to work in this regex...
            self.nvim.command(':call matchadd("LineNr", "^[0-9][0-9]*")')
            #self.nvim.command(':call matchadd("LineNr", "^~.*$")')
        except Exception as e:
            self.nvim.command(":close")
            raise e





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

            # save cur location in the tag stack (for the given window)
            win_id = self.tmp_work_window
            if win_id in self.marks:
                self.marks[win_id].append(Mark(curr_fpath, self.tmp_row, self.tmp_col))
            else:
                self.marks[win_id] = [Mark(curr_fpath, self.tmp_row, self.tmp_col)]
            #self.marks.append(Mark(curr_fpath, self.tmp_row, self.tmp_col))

        # get next location
        loc = self.tmp_saved_locations[x]

        # close menu window+buffer
        self.nvim.command(':close')

        # go to the saved off window
        self.nvim.request('nvim_set_current_win', self.tmp_work_window)

        # TODO probably need to do more escaping......
        path = '{}{}'.format(self.path, loc.path)
        path = path.replace("$", "\\$")

        # move that buffer to the location we want
        cmd = ':e +{line} {path}'.format(
            path=path,
            line=loc.line_num
        )
        self.nvim.command(cmd)

        self.tmp_saved_locations = None
        self.tmp_work_buffer = None
        self.tmp_work_window = None
        self.tmp_col = None
        self.tmp_row = None
        return

    @pynvim.command('OGrokDumpStack', nargs='0', range='')
    def OGrokDumpStack(self, args, range):
        self.nvim.out_write('OGrok: {}.\n'.format(self.marks))
        return

    @pynvim.command('OGrokJumpBack', nargs='0', range='')
    def OGrokJumpBack(self, args, range):
        win_id = self.nvim.request('nvim_get_current_win')
        if win_id not in self.marks:
            self.nvim.out_write('OGrok: no jump stack for this window.\n')
            return
        else:
            stack = self.marks[win_id]
            if len(stack) == 0:
                self.nvim.out_write('OGrok: jump stack is empty.\n')
                return

            m = stack.pop()
            # cursor(0, x) stays on current line and jumps to col x
            cmd = ':e +{line} {path} | call cursor(0,{col})'.format(
                    line=m.line, path=m.path, col=m.col+1)
            self.nvim.command(cmd)
