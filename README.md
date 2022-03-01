A neovim plugin that allows you to use an OpenGrok server in a way similar to
cscope.

Requires `nvim > 0.5.0` and `pynvim`.

Put `grokscope.py` in `~/.config/nvim/rplugin/python3` and then open `nvim` and
run `:UpdateRemotePlugins`

To use, put something similar to this in `~/.config/nvim/init.vim`

```vim
" base path of all the code indexed by opengrok
let srcpath = "/home/user/srcdir"
if stridx(getcwd(), srcpath) >= 0
    " for menu background color in opengrok plugin
    hi Pmenu ctermbg=lightblue guibg=lightblue

    " preset server and path
    autocmd VimEnter * OGrokSetServer   http://localhost:8080/source
    autocmd VimEnter * OGrokSetBasePath /home/user/srcdir

    " Search by symbol, definition, and file
    nmap <C-\>s :OGrok sym  <C-R>=expand("<cword>")<CR><CR> 0
    nmap <C-]>  :OGrok def  <C-R>=expand("<cword>")<CR><CR> 0
    nmap <C-\>f :OGrok file <C-R>=expand("<cfile>")<CR><CR> 0

    " tag navication is handled internally by the plugin, doesn't use the normal
    " tag stack
    nmap <C-t>  :OGrokJumpBack<CR>
endif
```
