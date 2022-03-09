A neovim plugin that allows you to use an OpenGrok server in a way similar to
cscope.

Requires `nvim > 0.5.0` and `pynvim`.

# Installing

Put `grokscope.py` in `~/.config/nvim/rplugin/python3` and then open `nvim` and
run `:UpdateRemotePlugins`. Restart open instances of `nvim`.



# Uninstalling

Remove `grokscope.py` from `~/.config/nvim/rplugin/python3` and then open `nvim` and
run `:UpdateRemotePlugins`. Restart open instances of `nvim`.

# Usage

There are two setup commands. `OGrokSetServer` and `OGrokSetBasePath`. They tell
the plugin where the opengrok server and corresponding local source code are
located.

```vim
OGrokSetServer   http://localhost:8080/source
OGrokSetBasePath /home/user/srcdir
```

The search command is `OGrok`. It currently supports three search types `sym`,
`def`, and `file` which correspond roughly to `s`, `g`, and `f` in cscope,
respectively.

E.g., to find the definition of `my_symbol`, you could use
```vim
OGrok def my_symbol
```

The `OGrok` command takes two optional positional flags, `fuzzy` and
`filter_project`.

Enabling the `fuzzy` flag (default: false) globs around the search term. I.e
```vim
OGrok def my_symbol 1
```
will also search for definitions matching `*my_symbol*`.

The `filter_project` flag (default: false) will cause the search to only return
results in the current project. The current project name is determined based on
`getcwd()` and the base path set by `OGrokSetBasePath`. E.g.,
```vim
OGrok def my_symbol 0 1
```

The tag navigation command is `OGrokJumpBack`. It jumps you back along the
plugin's internally managed jump stack.

# Automating Setup

It may be useful to put something similar to this in `~/.config/nvim/init.vim`

```vim
" base path of all the code indexed by opengrok
let srcpath = "/home/user/srcdir"
if stridx(getcwd(), srcpath) >= 0
    " for menu background color in opengrok plugin
    " hi Pmenu ctermbg=lightblue guibg=lightblue

    " preset server and path
    autocmd VimEnter * OGrokSetServer   http://localhost:8080/source
    autocmd VimEnter * OGrokSetBasePath /home/user/srcdir

    " Search by symbol, definition, and file
    " similar to the setup given in :help cscope
    nmap <C-\>s :OGrok sym  <C-R>=expand("<cword>") 0 1<CR><CR>
    nmap <C-]>  :OGrok def  <C-R>=expand("<cword>") 0 1<CR><CR>
    nmap <C-\>f :OGrok file <C-R>=expand("<cword>") 0 1<CR><CR>

    " may want <cfile> instead depending on the behavior you're looking for
    " nmap <C-\>f :OGrok file <C-R>=expand("<cfile>") 0 1<CR><CR>

    " tag navigation is handled internally by the plugin, doesn't use the normal
    " tag stack
    nmap <C-t>  :OGrokJumpBack<CR>
endif
```
