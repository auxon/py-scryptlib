import re
import json
import subprocess
from datetime import datetime
from pathlib import Path


SYNTAX_ERR_REG = '(?P<filePath>[^\s]+):(?P<line>\d+):(?P<column>\d+):\n([^\n]+\n){3}' \
        '(unexpected (?P<unexpected>[^\n]+)\nexpecting (?P<expecting>[^\n]+)|(?P<message>[^\n]+))'
SEMANTIC_ERR_REG = 'Error:(\s|\n)*(?P<filePath>[^\s]+):(?P<line>\d+):(?P<column>\d+):' \
        '(?P<line1>\d+):(?P<column1>\d+):*\n(?P<message>[^\n]+)\n'
INTERNAL_ERR_REG = 'Internal error:(?P<message>.+)'
WARNING_REG = 'Warning:(\s|\n)*(?P<filePath>[^\s]+):(?P<line>\d+):(?P<column>\d+):' \
        '(?P<line1>\d+):(?P<column1>\d+):*\n(?P<message>[^\n]+)\n'


class SyntaxErrorEntry:
    def __init__(self, message, message_full, got, expected, position, file_path):
        self.message = message
        self.message_full = message_full
        self.got = got
        self.expected = expected
        self.file_path = file_path
        self.position = position # (line, col)


class SemanticErrorEntry:
    def __init__(self, message, message_full, position_range, file_path):
        self.message = message
        self.message_full = message_full
        self.file_path = file_path
        self.position_range = position_range # [(line, col), (line1, col1)]


class EntryErrorBase(Exception):
    def __init__(self, error_entries):
        self.error_entries = error_entries
        message = []
        for error_entry in self.error_entries:
            message.append(error_entry.message_full)
        message = '\n'.join(message)
        super().__init__(message) 


class SyntaxError(EntryErrorBase):
    pass


class SemanticError(EntryErrorBase):
    pass


class InternalError(Exception):
    pass


class CompilerResult:
    def __init__(self,
            asm=[],
            ast=None,
            dep_ast=None,
            abi=None,
            warnings=[],
            compiler_version=None,
            contract=None,
            md5=None,
            structs=None,
            alias=None,
            source_file=None,
            auto_typed_vars=[]):
        self.asm = asm
        self.ast = ast
        self.dep_ast = dep_ast
        self.abi = abi
        self.warnings = warnings
        self.compiler_version = compiler_version
        self.contract = contract
        self.md5 = md5
        self.structs = structs
        self.alias = alias
        self.source_file = source_file
        self.auto_typed_vars = auto_typed_vars


def compile(source, **kwargs):
    asm = True
    debug = True
    optimize = False
    ast = False
    desc = False
    st = datetime.now()
    timeout = 1200
    out_files = dict()
    cmd_args = None
    from_file = isinstance(source, Path)
    source_prefix = source.stem if from_file else 'stdin'
    cwd = Path('.')

    if 'asm' in kwargs:
        asm = kwargs['asm']
    if 'debug' in kwargs:
        asm = kwargs['debug']
    if 'optimize' in kwargs:
        asm = kwargs['optimize']
    if 'ast' in kwargs:
        ast = kwargs['asm']
    if 'desc' in kwargs:
        desc = kwargs['desc']
    if 'timeout' in kwargs:
        timeout = kwargs['timeout']
    if 'cmd_args' in kwargs:
        cmd_args = kwargs['cmd_args']
    if 'cwd' in kwargs:
        cwd = Path(kwargs['cwd'])

    if not 'out_dir' in kwargs:
        raise Exception('Missing argument "out_dir". No output directory specified.')
    out_dir = Path(kwargs['out_dir'])

    if not 'compiler_bin' in kwargs:
        raise Exception('Missing argument "compiler_bin". Path to compiler not specified.')
    compiler_bin = kwargs['compiler_bin']

    if from_file:
        source_uri = source.absolute().as_uri()
    else:
        source_uri = 'stdin'

    # Assemble compiler command
    cmd_buff = [compiler_bin, 'compile']
    if asm:
        cmd_buff.append('--asm')
    if ast or desc:
        cmd_buff.append('--ast')
    if debug:
        cmd_buff.append('--debug')
    if optimize:
        cmd_buff.append('--opt')
    cmd_buff.append('-r')
    cmd_buff.append('-o')
    cmd_buff.append('{}'.format(str(out_dir.absolute())))
    if cmd_args:
        cmd_buff.append(cmd_args)
    if from_file:
        cmd_buff.append(str(source))

    # Execute compiler
    res = subprocess.run(cmd_buff, 
            stdout=subprocess.PIPE, 
            input=None if from_file else source.encode('utf-8'),
            timeout=timeout,
            cwd=cwd
        ).stdout

    # If compiling on win32, the outputs will be CRLF seperated.
    # We replace CRLF with LF, to make SYNTAX_ERR_REG、SEMANTIC_ERR_REG、IMPORT_ERR_REG work.
    res = res.replace(b'\r\n', b'\n').decode('utf-8')

    # Check compiler output for errors and raise exception if needed.
    __check_for_errors(res)

    # Collect warnings from compiler output.
    warnings = __get_warnings(res)

    compiler_result_params = dict()
    out_files = dict()

    if ast or desc:
        out_file_ast = out_dir / '{}_ast.json'.format(source_prefix)
        out_files['ast'] = out_file_ast
        ast_obj = __load_ast(out_file_ast)
        # Change source file paths to URIs
        __ast_filepaths_to_uris(ast_obj)
        ast_root = ast_obj[source_uri]
        static_consts = __ast_get_static_const_int_declarations(ast_obj)
        aliases = __ast_get_aliases(ast_obj)
        compiler_result_params['alias'] = aliases
        compiler_result_params['source_file'] = source_uri
        compiler_result_params['ast'] = ast_root
        compiler_result_params['abi'] = __ast_get_abi_declaration(ast_root, aliases, static_consts)
        del ast_obj[source_uri]
        compiler_result_params['dep_ast'] = ast_obj

    return CompilerResult(**compiler_result_params)


def __ast_filepaths_to_uris(asts):
    keys = list(asts.keys())
    for key in keys:
        if not key == 'stdin':
            source_uri = Path(key).absolute().as_uri()
            asts[source_uri] = asts.pop(key)


def __load_ast(file_ast):
    with open(file_ast, 'r', encoding='utf-8') as f:
        ast_obj = json.load(f)
    return ast_obj


def __ast_get_aliases(asts):
    res = []
    for ast in asts.values():
        for alias in ast['alias']:
            res.append({
                'name': alias['alias'],
                'type': alias['type']
                })
    return res


def __ast_get_static_const_int_declarations(asts):
    res = dict()
    for ast in asts.values():
        contracts = ast['contracts']
        for contract in contracts:
            contract_name = contract['name']
            for static in contract['statics']:
                name = '{}.{}'.format(contract_name, static['name'])
                value = static['expr']['value']
                res[name] = value
    return res


def __ast_get_abi_declaration(ast, aliases, static_consts):
    main_contract = ast['contracts'][-1]
    print(main_contract)


def __check_for_errors(compiler_output):
    # TODO: missing output folder: 
    # "scryptc: /tmp/scryptlib/stdin_asm.json: openFile: does not exist (No such file or directory)"
    if compiler_output.startswith('Error:'):
        match = re.search(INTERNAL_ERR_REG, compiler_output)
        if match:
            raise InternalError('Compiler internal error: {}'.format(match.group('message')))

        syntax_err_entries = []
        for match in re.finditer(SYNTAX_ERR_REG, compiler_output):
            file_path = match.group('filePath')
            got = match.group('unexpected')
            expected = match.group('expecting')
            line = int(match.group('line'))
            col = int(match.group('column'))
            message = match.group('message')
            message_full = match.string

            error_entry = SyntaxErrorEntry(message, message_full, got, expected, (line, col), file_path)
            syntax_err_entries.append(error_entry)

        if len(syntax_err_entries) > 0:
            raise SyntaxError(syntax_err_entries)

        semantic_err_entries = []
        for match in re.finditer(SEMANTIC_ERR_REG, compiler_output):
            file_path = match.group('filePath')
            line = int(match.group('line'))
            col = int(match.group('column'))
            line1 = int(match.group('line1'))
            col1 = int(match.group('column1'))
            position_range = [(line, col), (line1, col1)]
            message = match.group('message')
            message_sub_reg = 'Symbol `(?P<varName>\w+)` already defined at (?P<fileIndex>[^\s]+)' \
                    ':(?P<line>\d+):(?P<column>\d+):(?P<line1>\d+):(?P<column1>\d+)'
            message = re.sub(message_sub_reg, 'Symbol `$1` already defined at $3:$4:$5:$6', message)
            message_full = match.string

            error_entry = SemanticErrorEntry(message, message_full, position_range, file_path)
            semantic_err_entries.append(error_entry)

        if len(semantic_err_entries) > 0:
            raise SemanticError(semantic_err_entries)

        raise Exception(compiler_output)


def __get_warnings(compiler_output):
    warnings = []
    for match in  re.finditer(WARNING_REG, compiler_output):
        file_path = match.group('filePath')

        line = int(match.group('line'))
        col = int(match.group('column'))
        line1 = int(match.group('line1'))
        col1 = int(match.group('column1'))

        message = match.group('message')
        message_sub_reg = 'Variable `(?<varName>\w+)` shadows existing binding at ' \
                '(?<fileIndex>[^\s]+):(?<line>\d+):(?<column>\d+):(?<line1>\d+):(?<column1>\d+)'
        message = re.sub(message_sub_reg, 'Variable `$1` shadows existing binding at $3:$4:$5:$6', message)

        warnings.append((file_path, [(line, col), (line1,col1)], message))
    return warnings


