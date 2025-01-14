# coding=utf-8

from __future__ import print_function
import glob
import itertools
import multiprocessing
import os
import pipes
import re
import shutil
import subprocess
import sys
import time
import unittest

from tools.shared import *
from runner import RunnerCore, path_from_root, get_zlib_library, get_bullet_library, no_wasm_backend
import tools.shared
import tools.line_endings
import tools.js_optimizer
import tools.tempfiles
import tools.duplicate_function_eliminator


class temp_directory(object):
  def __enter__(self):
    self.directory = tempfile.mkdtemp(prefix='emsripten_temp_', dir=TEMP_DIR)
    self.prev_cwd = os.getcwd()
    os.chdir(self.directory)
    return self.directory

  def __exit__(self, type, value, traceback):
      os.chdir(self.prev_cwd) # On Windows, we can't have CWD in the directory we're deleting
      try_delete(self.directory)

class clean_write_access_to_canonical_temp_dir(object):
  def __init__(self, dir=CANONICAL_TEMP_DIR):
    self.canonical_temp_dir = dir

  def clean_emcc_files_in_temp_dir(self):
    for x in os.listdir(self.canonical_temp_dir):
      if x.startswith('emcc-') or x.startswith('a.out'):
        os.unlink(os.path.join(self.canonical_temp_dir, x))

  def __enter__(self):
    self.CANONICAL_TEMP_DIR_exists = os.path.exists(self.canonical_temp_dir)
    if not self.CANONICAL_TEMP_DIR_exists:
      os.makedirs(self.canonical_temp_dir)
    else:
      # Delete earlier files in the canonical temp directory so that
      # previous leftover files don't have a possibility of confusing
      # the test result e.g. on failure of the actual task
      self.clean_emcc_files_in_temp_dir()

  def __exit__(self, type, value, traceback):
    if not self.CANONICAL_TEMP_DIR_exists:
      try_delete(self.canonical_temp_dir)
      pass
    else:
      self.clean_emcc_files_in_temp_dir()

class other(RunnerCore):
  # Utility to run a simple test in this suite. This receives a directory which
  # should contain a test.cpp and test.out files, compiles the cpp, and runs it
  # to verify the output, with optional compile and run arguments.
  # TODO: use in more places
  def do_other_test(self, dirname, emcc_args=[], run_args=[]):
    shutil.copyfile(path_from_root('tests', dirname, 'test.cpp'), 'test.cpp')
    run_process([PYTHON, EMCC, 'test.cpp'] + emcc_args)
    expected = open(path_from_root('tests', dirname, 'test.out')).read()
    seen = run_js('a.out.js', args=run_args) + '\n'
    self.assertContained(expected, seen)

  def test_emcc_v(self):
    for compiler in [EMCC, EMXX]:
      # -v, without input files
      output = run_process([PYTHON, compiler, '-v'], stdout=PIPE, stderr=PIPE)
      self.assertContained('''clang version %s''' % expected_llvm_version(), output.stderr.replace('\r', ''), output.stderr.replace('\r', ''))
      self.assertContained('''GNU''', output.stdout)
      self.assertNotContained('this is dangerous', output.stdout)
      self.assertNotContained('this is dangerous', output.stderr)

  def test_emcc_generate_config(self):
    for compiler in [EMCC, EMXX]:
      config_path = './emscripten_config'
      run_process([PYTHON, compiler, '--generate-config', config_path])
      assert os.path.exists(config_path), 'A config file should have been created at %s' % config_path
      config_contents = open(config_path).read()
      self.assertContained('EMSCRIPTEN_ROOT', config_contents)
      self.assertContained('LLVM_ROOT', config_contents)
      os.remove(config_path)

  def test_emcc_1(self):
    for compiler in [EMCC, EMXX]:
      shortcompiler = os.path.basename(compiler)
      suffix = '.c' if compiler == EMCC else '.cpp'

      # --version
      output = run_process([PYTHON, compiler, '--version'], stdout=PIPE, stderr=PIPE)
      output = output.stdout.replace('\r', '')
      self.assertContained('''emcc (Emscripten gcc/clang-like replacement)''', output)
      self.assertContained('''Copyright (C) 2014 the Emscripten authors (see AUTHORS.txt)
This is free and open source software under the MIT license.
There is NO warranty; not even for MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.
''', output)

      # --help
      output = run_process([PYTHON, compiler, '--help'], stdout=PIPE, stderr=PIPE)
      self.assertContained('Display this information', output.stdout)
      self.assertContained('Most clang options will work', output.stdout)

      # -dumpmachine
      output = run_process([PYTHON, compiler, '-dumpmachine'], stdout=PIPE, stderr=PIPE)
      self.assertContained(get_llvm_target(), output.stdout)

      # -dumpversion
      output = run_process([PYTHON, compiler, '-dumpversion'], stdout=PIPE, stderr=PIPE)
      self.assertEqual(EMSCRIPTEN_VERSION + os.linesep, output.stdout, 'results should be identical')

      # emcc src.cpp ==> writes a.out.js and a.out.wasm
      self.clear()
      output = run_process([PYTHON, compiler, path_from_root('tests', 'hello_world' + suffix)], stdout=PIPE, stderr=PIPE)
      assert len(output.stdout) == 0, output.stdout
      assert os.path.exists('a.out.js'), output.stdout + '\n' + output.stderr
      assert os.path.exists('a.out.wasm'), output.stdout + '\n' + output.stderr
      self.assertContained('hello, world!', run_js('a.out.js'))

      # properly report source code errors, and stop there
      self.clear()
      assert not os.path.exists('a.out.js')
      process = run_process([PYTHON, compiler, path_from_root('tests', 'hello_world_error' + suffix)], stdout=PIPE, stderr=PIPE, check=False)
      assert not os.path.exists('a.out.js'), 'compilation failed, so no output file is expected'
      assert len(process.stdout) == 0, process.stdout
      assert process.returncode is not 0, 'Failed compilation must return a nonzero error code!'
      self.assertNotContained('IOError', process.stderr) # no python stack
      self.assertNotContained('Traceback', process.stderr) # no python stack
      self.assertContained('error: invalid preprocessing directive', process.stderr)
      self.assertContained(["error: use of undeclared identifier 'cheez", "error: unknown type name 'cheez'"], process.stderr)
      self.assertContained('errors generated', process.stderr)
      assert 'compiler frontend failed to generate LLVM bitcode, halting' in process.stderr.split('errors generated.')[1]

  def test_emcc_2(self):
    for compiler in [EMCC, EMXX]:
      shortcompiler = os.path.basename(compiler)
      suffix = '.c' if compiler == EMCC else '.cpp'

      # emcc src.cpp -c    and   emcc src.cpp -o src.[o|bc] ==> should give a .bc file
      #      regression check: -o js should create "js", with bitcode content
      for args in [['-c'], ['-o', 'src.o'], ['-o', 'src.bc'], ['-o', 'src.so'], ['-o', 'js'], ['-O1', '-c', '-o', '/dev/null'], ['-O1', '-o', '/dev/null']]:
        print('-c stuff', args)
        if '/dev/null' in args and WINDOWS:
          print('skip because windows')
          continue
        target = args[1] if len(args) == 2 else 'hello_world.o'
        self.clear()
        proc = Popen([PYTHON, compiler, path_from_root('tests', 'hello_world' + suffix)] + args, stdout=PIPE, stderr=PIPE)
        proc.communicate()
        assert proc.returncode == 0, [proc.returncode, args]
        if args[-1] == '/dev/null':
          print('(no output)')
          continue
        syms = Building.llvm_nm(target)
        assert len(syms.defs) == 1 and 'main' in syms.defs, 'Failed to generate valid bitcode'
        if target == 'js': # make sure emcc can recognize the target as a bitcode file
          shutil.move(target, target + '.bc')
          target += '.bc'
        output = run_process([PYTHON, compiler, target, '-o', target + '.js'], stdout = PIPE, stderr = PIPE)
        assert len(output.stdout) == 0, output.stdout
        assert os.path.exists(target + '.js'), 'Expected %s to exist since args are %s : %s' % (target + '.js', str(args), output.stdout + '\n' + output.stderr)
        self.assertContained('hello, world!', run_js(target + '.js'))

  def test_emcc_3(self):
    for compiler in [EMCC, EMXX]:
      shortcompiler = os.path.basename(compiler)
      suffix = '.c' if compiler == EMCC else '.cpp'

      # handle singleton archives
      self.clear()
      Popen([PYTHON, compiler, path_from_root('tests', 'hello_world' + suffix), '-o', 'a.bc'], stdout=PIPE, stderr=PIPE).communicate()
      Popen([LLVM_AR, 'r', 'a.a', 'a.bc'], stdout=PIPE, stderr=PIPE).communicate()
      assert os.path.exists('a.a')
      output = run_process([PYTHON, compiler, 'a.a'])
      assert os.path.exists('a.out.js'), output
      self.assertContained('hello, world!', run_js('a.out.js'))

      # emcc src.ll ==> generates .js
      self.clear()
      output = run_process([PYTHON, compiler, path_from_root('tests', 'hello_world.ll')], stdout=PIPE, stderr=PIPE)
      assert len(output.stdout) == 0, output.stdout
      assert os.path.exists('a.out.js'), output.stdout + '\n' + output.stderr
      self.assertContained('hello, world!', run_js('a.out.js'))

      # emcc [..] -o [path] ==> should work with absolute paths
      try:
        for path in [os.path.abspath(os.path.join('..', 'file1.js')), os.path.join('b_dir', 'file2.js')]:
          print(path)
          self.clear(in_curr=True)
          os.chdir(self.get_dir())
          if not os.path.exists('a_dir'): os.mkdir('a_dir')
          os.chdir('a_dir')
          if not os.path.exists('b_dir'): os.mkdir('b_dir')
          # use single file so we don't have more files to clean up
          output = run_process([PYTHON, compiler, path_from_root('tests', 'hello_world.ll'), '-o', path, '-s', 'SINGLE_FILE=1'], stdout=PIPE, stderr=PIPE)
          print(output)
          assert os.path.exists(path), path + ' does not exist; ' + output.stdout + '\n' + output.stderr
          last = os.getcwd()
          os.chdir(os.path.dirname(path))
          self.assertContained('hello, world!', run_js(os.path.basename(path)))
          os.chdir(last)
          try_delete(path)
      finally:
        os.chdir(self.get_dir())
      self.clear()

  def test_emcc_4(self):
    for compiler in [EMCC, EMXX]:
      shortcompiler = os.path.basename(compiler)
      suffix = '.c' if compiler == EMCC else '.cpp'

      # Optimization: emcc src.cpp -o something.js [-Ox]. -O0 is the same as not specifying any optimization setting
      for params, opt_level, bc_params, closure, has_malloc in [ # bc params are used after compiling to bitcode
        (['-o', 'something.js'],                          0, None, 0, 1),
        (['-o', 'something.js', '-O0'],                   0, None, 0, 0),
        (['-o', 'something.js', '-O1'],                   1, None, 0, 0),
        (['-o', 'something.js', '-O1', '-g'],             1, None, 0, 0), # no closure since debug
        (['-o', 'something.js', '-O2'],                   2, None, 0, 1),
        (['-o', 'something.js', '-O2', '-g'],             2, None, 0, 0),
        (['-o', 'something.js', '-Os'],                   2, None, 0, 1),
        (['-o', 'something.js', '-O3'],                   3, None, 0, 1),
        # and, test compiling to bitcode first
        (['-o', 'something.bc'], 0, [],      0, 0),
        (['-o', 'something.bc', '-O0'], 0, [], 0, 0),
        (['-o', 'something.bc', '-O1'], 1, ['-O1'], 0, 0),
        (['-o', 'something.bc', '-O2'], 2, ['-O2'], 0, 0),
        (['-o', 'something.bc', '-O3'], 3, ['-O3'], 0, 0),
        (['-O1', '-o', 'something.bc'], 1, [], 0, 0),
        # non-wasm
        (['-s', 'WASM=0', '-o', 'something.js'],                          0, None, 0, 1),
        (['-s', 'WASM=0', '-o', 'something.js', '-O0'],                   0, None, 0, 0),
        (['-s', 'WASM=0', '-o', 'something.js', '-O1'],                   1, None, 0, 0),
        (['-s', 'WASM=0', '-o', 'something.js', '-O1', '-g'],             1, None, 0, 0), # no closure since debug
        (['-s', 'WASM=0', '-o', 'something.js', '-O2'],                   2, None, 0, 1),
        (['-s', 'WASM=0', '-o', 'something.js', '-O2', '-g'],             2, None, 0, 0),
        (['-s', 'WASM=0', '-o', 'something.js', '-Os'],                   2, None, 0, 1),
        (['-s', 'WASM=0', '-o', 'something.js', '-O3'],                   3, None, 0, 1),
        # and, test compiling to bitcode first
        (['-s', 'WASM=0', '-o', 'something.bc'],        0, ['-s', 'WASM=0'],        0, 0),
        (['-s', 'WASM=0', '-o', 'something.bc', '-O0'], 0, ['-s', 'WASM=0'],        0, 0),
        (['-s', 'WASM=0', '-o', 'something.bc', '-O1'], 1, ['-s', 'WASM=0', '-O1'], 0, 0),
        (['-s', 'WASM=0', '-o', 'something.bc', '-O2'], 2, ['-s', 'WASM=0', '-O2'], 0, 0),
        (['-s', 'WASM=0', '-o', 'something.bc', '-O3'], 3, ['-s', 'WASM=0', '-O3'], 0, 0),
        (['-s', 'WASM=0', '-O1', '-o', 'something.bc'], 1, ['-s', 'WASM=0'],        0, 0),
      ]:
        if 'WASM=0' in params and self.is_wasm_backend():
          continue
        print(params, opt_level, bc_params, closure, has_malloc)
        self.clear()
        keep_debug = '-g' in params
        args = [PYTHON, compiler, path_from_root('tests', 'hello_world_loop' + ('_malloc' if has_malloc else '') + '.cpp')] + params
        print('..', args)
        output = run_process(args, stdout=PIPE, stderr=PIPE)
        assert len(output.stdout) == 0, output.stdout
        if bc_params is not None:
          assert os.path.exists('something.bc'), output.stderr
          bc_args = [PYTHON, compiler, 'something.bc', '-o', 'something.js'] + bc_params
          print('....', bc_args)
          output = run_process(bc_args, stdout=PIPE, stderr=PIPE)
        assert os.path.exists('something.js'), output.stderr
        self.assertContained('hello, world!', run_js('something.js'))

        # Verify optimization level etc. in the generated code
        # XXX these are quite sensitive, and will need updating when code generation changes
        generated = open('something.js').read()
        main = self.get_func(generated, '_main') if 'function _main' in generated else generated
        assert 'new Uint16Array' in generated and 'new Uint32Array' in generated, 'typed arrays 2 should be used by default'
        assert 'SAFE_HEAP' not in generated, 'safe heap should not be used by default'
        assert ': while(' not in main, 'when relooping we also js-optimize, so there should be no labelled whiles'
        if closure:
          if opt_level == 0: assert '._main =' in generated, 'closure compiler should have been run'
          elif opt_level >= 1: assert '._main=' in generated, 'closure compiler should have been run (and output should be minified)'
        else:
          # closure has not been run, we can do some additional checks. TODO: figure out how to do these even with closure
          assert '._main = ' not in generated, 'closure compiler should not have been run'
          if keep_debug:
            assert ('switch (label)' in generated or 'switch (label | 0)' in generated) == (opt_level <= 0), 'relooping should be in opt >= 1'
            assert ('assert(STACKTOP < STACK_MAX' in generated) == (opt_level == 0), 'assertions should be in opt == 0'
          if 'WASM=0' in params:
            if opt_level >= 2 and '-g' in params:
              assert re.search('HEAP8\[\$?\w+ ?\+ ?\(+\$?\w+ ?', generated) or re.search('HEAP8\[HEAP32\[', generated) or re.search('[i$]\d+ & ~\(1 << [i$]\d+\)', generated), 'eliminator should create compound expressions, and fewer one-time vars' # also in -O1, but easier to test in -O2
            if opt_level == 0 or '-g' in params: assert 'function _main() {' in generated or 'function _main(){' in generated, 'Should be unminified'
            elif opt_level >= 2: assert ('function _main(){' in generated or '"use asm";var a=' in generated), 'Should be whitespace-minified'

  def test_emcc_5(self):
    for compiler in [EMCC, EMXX]:
      shortcompiler = os.path.basename(compiler)
      suffix = '.c' if compiler == EMCC else '.cpp'

      # asm.js optimization levels
      for params, test, text in [
        (['-O2'], lambda generated: 'function addRunDependency' in generated, 'shell has unminified utilities'),
        (['-O2', '--closure', '1'], lambda generated: 'function addRunDependency' not in generated and ';function' in generated, 'closure minifies the shell, removes whitespace'),
        (['-O2', '--closure', '1', '-g1'], lambda generated: 'function addRunDependency' not in generated and ';function' not in generated, 'closure minifies the shell, -g1 makes it keep whitespace'),
        (['-O2'], lambda generated: 'var b=0' in generated and not 'function _main' in generated, 'registerize/minify is run by default in -O2'),
        (['-O2', '--minify', '0'], lambda generated: 'var b = 0' in generated and not 'function _main' in generated, 'minify is cancelled, but not registerize'),
        (['-O2', '--js-opts', '0'], lambda generated: 'var b=0' not in generated and 'var b = 0' not in generated and 'function _main' in generated, 'js opts are cancelled'),
        (['-O2', '-g'], lambda generated: 'var b=0' not in generated and 'var b = 0' not in generated and 'function _main' in generated, 'registerize/minify is cancelled by -g'),
        (['-O2', '-g0'], lambda generated: 'var b=0'   in generated and not 'function _main' in generated, 'registerize/minify is run by default in -O2 -g0'),
        (['-O2', '-g1'], lambda generated: 'var b = 0' in generated and not 'function _main' in generated, 'compress is cancelled by -g1'),
        (['-O2', '-g2'], lambda generated: ('var b = 0' in generated or 'var i1 = 0' in generated) and 'function _main' in generated, 'minify is cancelled by -g2'),
        (['-O2', '-g3'], lambda generated: 'var b=0' not in generated and 'var b = 0' not in generated and 'function _main' in generated, 'registerize is cancelled by -g3'),
        (['-O2', '--profiling'], lambda generated: ('var b = 0' in generated or 'var i1 = 0' in generated) and 'function _main' in generated, 'similar to -g2'),
        (['-O2', '-profiling'],  lambda generated: ('var b = 0' in generated or 'var i1 = 0' in generated) and 'function _main' in generated, 'similar to -g2'),
        (['-O2', '--profiling-funcs'], lambda generated: 'var b=0' in generated and '"use asm";var a=' in generated and 'function _main' in generated, 'very minified, but retain function names'),
        (['-O2', '-profiling-funcs'],  lambda generated: 'var b=0' in generated and '"use asm";var a=' in generated and 'function _main' in generated, 'very minified, but retain function names'),
        (['-O2'],                      lambda generated: 'var b=0' in generated and '"use asm";var a=' in generated and 'function _main' not in generated, 'very minified, no function names'),
        #(['-O2', '-g4'], lambda generated: 'var b=0' not in generated and 'var b = 0' not in generated and 'function _main' in generated, 'same as -g3 for now'),
        (['-s', 'INLINING_LIMIT=0'], lambda generated: 'function _dump' in generated, 'no inlining without opts'),
        ([], lambda generated: 'Module["_dump"]' not in generated, 'dump is not exported by default'),
        (['-s', 'EXPORTED_FUNCTIONS=["_main", "_dump"]'], lambda generated: 'Module["_dump"]' in generated, 'dump is now exported'),
        (['--llvm-opts', '1'], lambda generated: '_puts(' in generated, 'llvm opts requested'),
        ([], lambda generated: '// Sometimes an existing Module' in generated, 'without opts, comments in shell code'),
        (['-O2'], lambda generated: '// Sometimes an existing Module' not in generated, 'with opts, no comments in shell code'),
        (['-O2', '-g2'], lambda generated: '// Sometimes an existing Module' not in generated, 'with -g2, no comments in shell code'),
        (['-O2', '-g3'], lambda generated: '// Sometimes an existing Module' in generated, 'with -g3, yes comments in shell code'),
      ]:
        print(params, text)
        self.clear()
        output = run_process([PYTHON, compiler, path_from_root('tests', 'hello_world_loop.cpp'), '-o', 'a.out.js', '-s', 'WASM=0'] + params, stdout=PIPE, stderr=PIPE)
        assert len(output.stdout) == 0, output.stdout
        assert os.path.exists('a.out.js'), output.stdout + '\n' + output.stderr
        self.assertContained('hello, world!', run_js('a.out.js'))
        assert test(open('a.out.js').read()), text

  def test_emcc_6(self):
    for compiler in [EMCC, EMXX]:
      shortcompiler = os.path.basename(compiler)
      suffix = '.c' if compiler == EMCC else '.cpp'

      # Compiling two source files into a final JS.
      for args, target in [([], 'a.out.js'), (['-o', 'combined.js'], 'combined.js')]:
        self.clear()
        output = run_process([PYTHON, compiler, path_from_root('tests', 'twopart_main.cpp'), path_from_root('tests', 'twopart_side.cpp')] + args,
                             stdout=PIPE, stderr=PIPE)
        assert len(output.stdout) == 0, output.stdout
        assert os.path.exists(target), output.stdout + '\n' + output.stderr
        self.assertContained('side got: hello from main, over', run_js(target))

        # Compiling two files with -c will generate separate .bc files
        self.clear()
        expect_error = '-o' in args # specifying -o and -c is an error
        output = run_process([PYTHON, compiler, path_from_root('tests', 'twopart_main.cpp'), path_from_root('tests', 'twopart_side.cpp'), '-c'] + args,
                             stdout=PIPE, stderr=PIPE, check=not expect_error)
        if expect_error:
          assert 'fatal error' in output.stderr, output.stderr
          continue

        assert os.path.exists('twopart_main.o'), output.stdout + '\n' + output.stderr
        assert os.path.exists('twopart_side.o'), output.stdout + '\n' + output.stderr
        assert not os.path.exists(target), 'We should only have created bitcode here: ' + output.stdout + '\n' + output.stderr

        # Compiling one of them alone is expected to fail
        output = run_process([PYTHON, compiler, 'twopart_main.o', '-O1', '-g'] + args, stdout=PIPE, stderr=PIPE)
        assert os.path.exists(target), output.stdout + '\n' + output.stderr
        #print output.stdout + '\n' + output.stderr
        self.assertContained('missing function', run_js(target, stderr=STDOUT, assert_returncode=None))
        try_delete(target)

        # Combining those bc files into js should work
        output = run_process([PYTHON, compiler, 'twopart_main.o', 'twopart_side.o'] + args, stdout=PIPE, stderr=PIPE)
        assert os.path.exists(target), output.stdout + '\n' + output.stderr
        self.assertContained('side got: hello from main, over', run_js(target))

        # Combining bc files into another bc should also work
        try_delete(target)
        assert not os.path.exists(target)
        output = run_process([PYTHON, compiler, 'twopart_main.o', 'twopart_side.o', '-o', 'combined.bc'] + args, stdout=PIPE, stderr=PIPE)
        syms = Building.llvm_nm('combined.bc')
        assert len(syms.defs) == 2 and 'main' in syms.defs, 'Failed to generate valid bitcode'
        output = run_process([PYTHON, compiler, 'combined.bc', '-o', 'combined.bc.js'], stdout = PIPE, stderr = PIPE)
        assert len(output.stdout) == 0, output.stdout
        assert os.path.exists('combined.bc.js'), 'Expected %s to exist' % ('combined.bc.js')
        self.assertContained('side got: hello from main, over', run_js('combined.bc.js'))

  def test_emcc_7(self):
    for compiler in [EMCC, EMXX]:
      shortcompiler = os.path.basename(compiler)
      suffix = '.c' if compiler == EMCC else '.cpp'

      # --js-transform <transform>
      self.clear()
      trans = os.path.join(self.get_dir(), 't.py')
      trans_file = open(trans, 'w')
      trans_file.write('''
import sys
f = open(sys.argv[1], 'a')
f.write('transformed!')
f.close()
''')
      trans_file.close()
      output = Popen([PYTHON, compiler, path_from_root('tests', 'hello_world' + suffix), '--js-transform', '%s t.py' % (PYTHON)], stdout=PIPE, stderr=PIPE).communicate()
      assert 'transformed!' in open('a.out.js').read(), 'Transformed output must be as expected'

      for opts in [0, 1, 2, 3]:
        print('mem init in', opts)
        self.clear()
        output = Popen([PYTHON, compiler, path_from_root('tests', 'hello_world.c'), '-s', 'WASM=0', '-O' + str(opts)], stdout=PIPE, stderr=PIPE).communicate()
        assert os.path.exists('a.out.js.mem') == (opts >= 2), 'mem file should exist in -O2+'

  def test_emcc_asm_v_wasm(self):
    for opts in ([], ['-O1'], ['-O2'], ['-O3']):
      print('opts', opts)
      for mode in ([], ['-s', 'WASM=0'], ['-s', 'BINARYEN=0'], ['-s', 'WASM=1'], ['-s', 'BINARYEN=1']):
        self.clear()
        wasm = '=0' not in str(mode)
        print('  mode', mode, 'wasm?', wasm)
        run_process([PYTHON, EMCC, path_from_root('tests', 'hello_world.c')] + opts + mode)
        assert os.path.exists('a.out.js')
        assert os.path.exists('a.out.wasm') == wasm
        for engine in JS_ENGINES:
          print('    engine', engine)
          out = run_js('a.out.js', engine=engine, stderr=PIPE, full_output=True)
          self.assertContained('hello, world!', out)
          if not wasm and engine == SPIDERMONKEY_ENGINE: self.validate_asmjs(out)
        if not wasm:
          src = open('a.out.js').read()
          if opts == []:
            assert 'almost asm' in src
          else:
            assert 'use asm' in src

  # Test that if multiple processes attempt to access or build stuff to the cache on demand, that exactly one of the processes
  # will, and the other processes will block to wait until that process finishes.
  def test_emcc_multiprocess_cache_access(self):
    with temp_directory() as tempdirname:
      c_file = os.path.join(tempdirname, 'test.c')
      open(c_file, 'w').write(r'''
        #include <stdio.h>
        int main() {
          printf("hello, world!\n");
          return 0;
        }
        ''')
      cache_dir_name = os.path.join(tempdirname, 'emscripten_cache')
      tasks = []
      num_times_libc_was_built = 0
      for i in range(3):
        p = subprocess.Popen([PYTHON, EMCC, c_file, '--cache', cache_dir_name, '-o', '%d.js' % i], stderr=subprocess.STDOUT, stdout=PIPE, universal_newlines=True)
        tasks += [p]
      for p in tasks:
        stdout, stderr = p.communicate()
        print('stdout:\n', stdout)
        print('stderr:\n', stderr)
        assert not p.returncode, 'A child process failed with return code %s: %s' % (p.returncode, stderr)
        if 'generating system library: libc.bc' in stdout:
          num_times_libc_was_built += 1
      assert os.path.exists(cache_dir_name), 'The cache directory %s must exist after the build' % cache_dir_name
      assert os.path.exists(os.path.join(cache_dir_name, 'asmjs', 'libc.bc')), 'The cache directory must contain a built libc'
      assert num_times_libc_was_built == 1, 'Exactly one child process should have triggered libc build! (instead %d processes did)' % num_times_libc_was_built

  def test_emcc_cache_flag(self):
    with temp_directory() as tempdirname:
      c_file = os.path.join(tempdirname, 'test.c')
      cache_dir_name = os.path.join(tempdirname, 'emscripten_cache')
      assert os.path.exists(cache_dir_name) == False, 'The cache directory %s must not already exist' % cache_dir_name
      open(c_file, 'w').write(r'''
        #include <stdio.h>
        int main() {
          printf("hello, world!\n");
          return 0;
        }
        ''')
      subprocess.check_call([PYTHON, EMCC, c_file, '--cache', cache_dir_name])
      assert os.path.exists(cache_dir_name), 'The cache directory %s must exist after the build' % cache_dir_name
      assert os.path.exists(os.path.join(cache_dir_name, 'asmjs', 'libc.bc')), 'The cache directory must contain a built libc'

  def test_emcc_cflags(self):
    # see we print them out
    with clean_write_access_to_canonical_temp_dir(self.canonical_temp_dir): # --cflags needs to set EMCC_DEBUG=1, which needs to create canonical temp directory.
      output = run_process([PYTHON, EMCC, '--cflags'], stdout=PIPE, stderr=PIPE)
    flags = output.stdout.strip()
    self.assertContained(' '.join(Building.doublequote_spaces(COMPILER_OPTS)), flags)
    # check they work
    cmd = [CLANG, path_from_root('tests', 'hello_world.cpp')] + shlex.split(flags.replace('\\', '\\\\')) + ['-c', '-emit-llvm', '-o', 'a.bc']
    subprocess.check_call(cmd)
    subprocess.check_call([PYTHON, EMCC, 'a.bc'])
    self.assertContained('hello, world!', run_js(self.in_dir('a.out.js')))

  def test_emar_em_config_flag(self):
    # We expand this in case the EM_CONFIG is ~/.emscripten (default)
    config = os.path.expanduser(EM_CONFIG)
    # We pass -version twice to work around the newargs > 2 check in emar
    output = run_process([PYTHON, EMAR, '--em-config', config, '-version', '-version'], stdout=PIPE, stderr=PIPE)
    assert output.stdout
    assert not output.stderr
    self.assertContained('LLVM', output.stdout)

  def test_cmake(self):
    # Test all supported generators.
    if WINDOWS:
      generators = ['MinGW Makefiles', 'NMake Makefiles']
    else:
      generators = ['Unix Makefiles', 'Ninja', 'Eclipse CDT4 - Ninja']

    def nmake_detect_error(configuration):
      if Building.which(configuration['build'][0]):
        return None
      else:
        return 'Skipping NMake test for CMake support, since nmake was not found in PATH. Run this test in Visual Studio command prompt to easily access nmake.'

    def check_makefile(configuration, dirname):
      assert os.path.exists(dirname + '/Makefile'), 'CMake call did not produce a Makefile!'

    configurations = { 'MinGW Makefiles'     : { 'prebuild': check_makefile,
                                                 'build'   : ['mingw32-make'],

                       },
                       'NMake Makefiles'     : { 'detect'  : nmake_detect_error,
                                                 'prebuild': check_makefile,
                                                 'build'   : ['nmake', '/NOLOGO'],
                       },
                       'Unix Makefiles'      : { 'prebuild': check_makefile,
                                                 'build'   : ['make'],
                       },
                       'Ninja'               : { 'build'   : ['ninja'],
                       },
                       'Eclipse CDT4 - Ninja': { 'build'   : ['ninja'],
                       }
    }

    if os.name == 'nt':
      emconfigure = path_from_root('emconfigure.bat')
    else:
      emconfigure = path_from_root('emconfigure')

    for generator in generators:
      conf = configurations[generator]

      make = conf['build']

      try:
        detector = conf['detect']
      except KeyError:
        detector = None

      if detector:
        error = detector(conf)
      elif len(make) == 1 and not Building.which(make[0]):
        # Use simple test if applicable
        error = 'Skipping %s test for CMake support, since it could not be detected.' % generator
      else:
        error = None

      if error:
        logging.warning(error)
        continue

      try:
        prebuild = conf['prebuild']
      except KeyError:
        prebuild = None

      try:
        postbuild = conf['postbuild']
      except KeyError:
        postbuild = None

      # ('directory to the test', 'output filename', ['extra args to pass to CMake'])
      # Testing all combinations would be too much work and the test would take 10 minutes+ to finish (CMake feature detection is slow),
      # so combine multiple features into one to try to cover as much as possible while still keeping this test in sensible time limit.
      cases = [
        ('target_js',      'test_cmake.js',         ['-DCMAKE_BUILD_TYPE=Debug']),
        ('target_html',    'hello_world_gles.html', ['-DCMAKE_BUILD_TYPE=Release',        '-DBUILD_SHARED_LIBS=OFF']),
        ('target_library', 'libtest_cmake.a',       ['-DCMAKE_BUILD_TYPE=MinSizeRel',     '-DBUILD_SHARED_LIBS=OFF']),
        ('target_library', 'libtest_cmake.a',       ['-DCMAKE_BUILD_TYPE=RelWithDebInfo', '-DCPP_LIBRARY_TYPE=STATIC']),
        ('target_library', 'libtest_cmake.so',      ['-DCMAKE_BUILD_TYPE=Release',        '-DBUILD_SHARED_LIBS=ON']),
        ('target_library', 'libtest_cmake.so',      ['-DCMAKE_BUILD_TYPE=Release',        '-DBUILD_SHARED_LIBS=ON', '-DCPP_LIBRARY_TYPE=SHARED']),
        ('stdproperty',    'helloworld.js',         [])
      ]
      for test_dir, output_file, cmake_args in cases:
        cmakelistsdir = path_from_root('tests', 'cmake', test_dir)
        with temp_directory() as tempdirname:
          # Run Cmake
          cmd = [emconfigure, 'cmake'] + cmake_args + ['-G', generator, cmakelistsdir]

          env = os.environ.copy()
          # https://github.com/kripken/emscripten/pull/5145: Check that CMake works even if EMCC_SKIP_SANITY_CHECK=1 is passed.
          if test_dir == 'target_html':
            env['EMCC_SKIP_SANITY_CHECK'] = '1'
          print(str(cmd))
          ret = run_process(cmd, env=env, stdout=None if EM_BUILD_VERBOSE >= 2 else PIPE, stderr=None if EM_BUILD_VERBOSE >= 1 else PIPE)
          if ret.stderr != None and len(ret.stderr.strip()):
            logging.error(ret.stderr) # If there were any errors, print them directly to console for diagnostics.
          if ret.stderr != None and 'error' in ret.stderr.lower():
            logging.error('Failed command: ' + ' '.join(cmd))
            logging.error('Result:\n' + ret.stderr)
            raise Exception('cmake call failed!')

          if prebuild:
            prebuild(configuration, tempdirname)

          # Build
          cmd = make
          if EM_BUILD_VERBOSE >= 3 and 'Ninja' not in generator:
            cmd += ['VERBOSE=1']
          ret = run_process(cmd, stdout=None if EM_BUILD_VERBOSE >= 2 else PIPE)
          if ret.stderr != None and len(ret.stderr.strip()):
            logging.error(ret.stderr) # If there were any errors, print them directly to console for diagnostics.
          if ret.stdout != None and 'error' in ret.stdout.lower() and not '0 error(s)' in ret.stdout.lower():
            logging.error('Failed command: ' + ' '.join(cmd))
            logging.error('Result:\n' + ret.stdout)
            raise Exception('make failed!')
          assert os.path.exists(tempdirname + '/' + output_file), 'Building a cmake-generated Makefile failed to produce an output file %s!' % tempdirname + '/' + output_file

          if postbuild:
            postbuild(configuration, tempdirname)

          # Run through node, if CMake produced a .js file.
          if output_file.endswith('.js'):
            ret = run_process(NODE_JS + [tempdirname + '/' + output_file], stdout=PIPE).stdout
            self.assertTextDataIdentical(open(cmakelistsdir + '/out.txt', 'r').read().strip(), ret.strip())

  # Test that the various CMAKE_xxx_COMPILE_FEATURES that are advertised for the Emscripten toolchain match with the actual language features that Clang supports.
  # If we update LLVM version and this test fails, copy over the new advertised features from Clang and place them to cmake/Modules/Platform/Emscripten.cmake.
  def test_cmake_compile_features(self):
    if WINDOWS: self.skipTest('Skipped on Windows because CMake does not configure native Clang builds well on Windows.')

    with temp_directory():
      cmd = ['cmake', '-DCMAKE_C_COMPILER=' + CLANG_CC, '-DCMAKE_CXX_COMPILER=' + CLANG_CPP, path_from_root('tests', 'cmake', 'stdproperty')]
      print(str(cmd))
      native_features = run_process(cmd, stdout=PIPE).stdout

    if os.name == 'nt': emconfigure = path_from_root('emcmake.bat')
    else: emconfigure = path_from_root('emcmake')

    with temp_directory():
      cmd = [emconfigure, 'cmake', path_from_root('tests', 'cmake', 'stdproperty')]
      print(str(cmd))
      emscripten_features = run_process(cmd, stdout=PIPE).stdout

    native_features = '\n'.join([x for x in native_features.split('\n') if '***' in x])
    emscripten_features = '\n'.join([x for x in emscripten_features.split('\n') if '***' in x])
    self.assertTextDataIdentical(native_features, emscripten_features)

  # Tests that it's possible to pass C++11 or GNU++11 build modes to CMake by building code that needs C++11 (embind)
  def test_cmake_with_embind_cpp11_mode(self):
    cwd = os.getcwd()

    for args in [[], ['-DNO_GNU_EXTENSIONS=1']]:
      with temp_directory() as tempdirname:
        configure = [path_from_root('emcmake.bat' if WINDOWS else 'emcmake'), 'cmake', path_from_root('tests', 'cmake', 'cmake_with_emval')] + args
        print(str(configure))
        subprocess.check_call(configure)
        build = ['cmake', '--build', '.']
        print(str(build))
        subprocess.check_call(build)

        ret = run_process(NODE_JS + [os.path.join(tempdirname, 'cpp_with_emscripten_val.js')], stdout=PIPE).stdout.strip()
        if '-DNO_GNU_EXTENSIONS=1' in args:
          self.assertTextDataIdentical('Hello! __STRICT_ANSI__: 1, __cplusplus: 201103', ret)
        else:
          self.assertTextDataIdentical('Hello! __STRICT_ANSI__: 0, __cplusplus: 201103', ret)

  # Tests that the Emscripten CMake toolchain option -DEMSCRIPTEN_GENERATE_BITCODE_STATIC_LIBRARIES=ON works.
  def test_cmake_bitcode_static_libraries(self):
    if os.name == 'nt': emcmake = path_from_root('emcmake.bat')
    else: emcmake = path_from_root('emcmake')

    # Test that building static libraries by default generates UNIX archives (.a, with the emar tool)
    with temp_directory() as tempdirname:
      subprocess.check_call([emcmake, 'cmake', path_from_root('tests', 'cmake', 'static_lib')])
      subprocess.check_call([Building.which('cmake'), '--build', '.'])
      assert tools.shared.Building.is_ar(os.path.join(tempdirname, 'libstatic_lib.a'))
      assert tools.shared.Building.is_bitcode(os.path.join(tempdirname, 'libstatic_lib.a'))

    # Test that passing the -DEMSCRIPTEN_GENERATE_BITCODE_STATIC_LIBRARIES=ON directive causes CMake to generate LLVM bitcode files as static libraries (.bc)
    with temp_directory() as tempdirname:
      subprocess.check_call([emcmake, 'cmake', '-DEMSCRIPTEN_GENERATE_BITCODE_STATIC_LIBRARIES=ON', path_from_root('tests', 'cmake', 'static_lib')])
      subprocess.check_call([Building.which('cmake'), '--build', '.'])
      assert tools.shared.Building.is_bitcode(os.path.join(tempdirname, 'libstatic_lib.bc'))
      assert not tools.shared.Building.is_ar(os.path.join(tempdirname, 'libstatic_lib.bc'))

    # Test that one is able to fake custom suffixes for static libraries.
    # (sometimes projects want to emulate stuff, and do weird things like files with ".so" suffix which are in fact either ar archives or bitcode files)
    with temp_directory() as tempdirname:
      subprocess.check_call([emcmake, 'cmake', '-DSET_FAKE_SUFFIX_IN_PROJECT=1', path_from_root('tests', 'cmake', 'static_lib')])
      subprocess.check_call([Building.which('cmake'), '--build', '.'])
      assert tools.shared.Building.is_bitcode(os.path.join(tempdirname, 'myprefix_static_lib.somecustomsuffix'))
      assert tools.shared.Building.is_ar(os.path.join(tempdirname, 'myprefix_static_lib.somecustomsuffix'))

  # Tests that the CMake variable EMSCRIPTEN_VERSION is properly provided to user CMake scripts
  def test_cmake_emscripten_version(self):
    if os.name == 'nt': emcmake = path_from_root('emcmake.bat')
    else: emcmake = path_from_root('emcmake')

    with temp_directory() as tempdirname:
      subprocess.check_call([emcmake, 'cmake', path_from_root('tests', 'cmake', 'emscripten_version')])

  def test_failure_error_code(self):
    for compiler in [EMCC, EMXX]:
      # Test that if one file is missing from the build, then emcc shouldn't succeed, and shouldn't try to produce an output file.
      process = Popen([PYTHON, compiler, path_from_root('tests', 'hello_world.c'), 'this_file_is_missing.c', '-o', 'this_output_file_should_never_exist.js'], stdout=PIPE, stderr=PIPE)
      process.communicate()
      assert process.returncode is not 0, 'Trying to compile a nonexisting file should return with a nonzero error code!'
      assert os.path.exists('this_output_file_should_never_exist.js') == False, 'Emcc should not produce an output file when build fails!'

  def test_use_cxx(self):
    open('empty_file', 'w').write(' ')
    try:
      dash_xc = run_process([PYTHON, EMCC, '-v', '-xc', 'empty_file'], stdout=PIPE, stderr=PIPE).stderr
      self.assertNotContained('-std=c++03', dash_xc)
      dash_xcpp = run_process([PYTHON, EMCC, '-v', '-xc++', 'empty_file'], stdout=PIPE, stderr=PIPE).stderr
      self.assertContained('-std=c++03', dash_xcpp)
    finally:
      try_delete('empty_file')

  def test_cxx03(self):
    for compiler in [EMCC, EMXX]:
      process = Popen([PYTHON, compiler, path_from_root('tests', 'hello_cxx03.cpp')], stdout=PIPE, stderr=PIPE)
      process.communicate()
      assert process.returncode is 0, 'By default, emscripten should build using -std=c++03!'

  def test_cxx11(self):
    for std in ['-std=c++11', '--std=c++11']:
      for compiler in [EMCC, EMXX]:
        process = Popen([PYTHON, compiler, std, path_from_root('tests', 'hello_cxx11.cpp')], stdout=PIPE, stderr=PIPE)
        process.communicate()
        assert process.returncode is 0, 'User should be able to specify custom -std= on the command line!'

  # Regression test for issue #4522: Incorrect CC vs CXX detection
  def test_incorrect_c_detection(self):
    with open('test.c', 'w') as f:
      f.write('foo\n')
    for compiler in [EMCC, EMXX]:
      run_process([PYTHON, compiler, '--bind', '--embed-file', 'test.c', path_from_root('tests', 'hello_world.cpp')])

  def test_odd_suffixes(self):
    for suffix in ['CPP', 'c++', 'C++', 'cxx', 'CXX', 'cc', 'CC', 'i', 'ii']:
      self.clear()
      print(suffix)
      shutil.copyfile(path_from_root('tests', 'hello_world.c'), 'test.' + suffix)
      Popen([PYTHON, EMCC, os.path.join(self.get_dir(), 'test.' + suffix)]).communicate()
      self.assertContained('hello, world!', run_js(os.path.join(self.get_dir(), 'a.out.js')))

    for suffix in ['lo']:
      self.clear()
      print(suffix)
      Popen([PYTHON, EMCC, path_from_root('tests', 'hello_world.c'), '-o', 'binary.' + suffix]).communicate()
      Popen([PYTHON, EMCC, 'binary.' + suffix]).communicate()
      self.assertContained('hello, world!', run_js(os.path.join(self.get_dir(), 'a.out.js')))

  def test_catch_undef(self):
    open(os.path.join(self.get_dir(), 'test.cpp'), 'w').write(r'''
      #include <vector>
      #include <stdio.h>

      class Test {
      public:
        std::vector<int> vector;
      };

      Test globalInstance;

      int main() {
        printf("hello, world!\n");
        return 0;
      }
    ''')
    Popen([PYTHON, EMCC, os.path.join(self.get_dir(), 'test.cpp'), '-fsanitize=undefined']).communicate()
    self.assertContained('hello, world!', run_js(os.path.join(self.get_dir(), 'a.out.js')))

  @no_wasm_backend()
  def test_asm_minify(self):
    def test(args):
      Popen([PYTHON, EMCC, path_from_root('tests', 'hello_world_loop_malloc.cpp'), '-s', 'WASM=0'] + args).communicate()
      self.assertContained('hello, world!', run_js(self.in_dir('a.out.js')))
      return open(self.in_dir('a.out.js')).read()

    src = test([])
    assert 'function _malloc' in src

    src = test(['-O2', '-s', 'ASM_JS=1'])
    normal_size = len(src)
    print('normal', normal_size)
    assert 'function _malloc' not in src

    src = test(['-O2', '-s', 'ASM_JS=1', '--minify', '0'])
    unminified_size = len(src)
    print('unminified', unminified_size)
    assert unminified_size > normal_size
    assert 'function _malloc' not in src

    src = test(['-O2', '-s', 'ASM_JS=1', '-g'])
    debug_size = len(src)
    print('debug', debug_size)
    assert debug_size > unminified_size
    assert 'function _malloc' in src

  def test_dangerous_func_cast(self):
    src = r'''
      #include <stdio.h>
      typedef void (*voidfunc)();
      int my_func() {
        printf("my func\n");
        return 10;
      }
      int main(int argc, char **argv) {
        voidfunc fps[10];
        for (int i = 0; i < 10; i++) fps[i] = (i == argc) ? (void (*)())my_func : NULL;
        fps[2*(argc-1) + 1]();
        return 0;
      }
    '''
    open('src.c', 'w').write(src)
    def test(args, expected, moar_expected=None):
      print(args, expected, moar_expected)
      out, err = Popen([PYTHON, EMCC, 'src.c'] + args, stderr=PIPE).communicate()
      self.assertContained(expected, run_js(self.in_dir('a.out.js'), stderr=PIPE, full_output=True, assert_returncode=None))
      print('in asm.js')
      if self.is_wasm_backend():
        return
      out, err = Popen([PYTHON, EMCC, 'src.c', '-s', 'WASM=0'] + args, stderr=PIPE).communicate()
      self.assertContained(expected, run_js(self.in_dir('a.out.js'), stderr=PIPE, full_output=True, assert_returncode=None))
      # TODO: emulation function support in wasm is imperfect
      print('with emulated function pointers in asm.js')
      Popen([PYTHON, EMCC, '-s', 'WASM=0', 'src.c', '-s', 'BINARYEN_ASYNC_COMPILATION=0'] + args + ['-s', 'EMULATED_FUNCTION_POINTERS=1'], stderr=PIPE).communicate()
      out = run_js(self.in_dir('a.out.js'), stderr=PIPE, full_output=True, assert_returncode=None)
      self.assertContained(expected, out)
      if moar_expected: self.assertContained(moar_expected, out)

    # fastcomp. all asm, so it can't just work with wrong sigs. but, ASSERTIONS=2 gives much better info to debug
    test(['-O1'], 'Build with -s ASSERTIONS=1 for more info.') # no useful info, but does mention ASSERTIONS
    test(['-O1', '-s', 'ASSERTIONS=1'], '''Invalid function pointer called with signature 'v'. Perhaps this is an invalid value (e.g. caused by calling a virtual method on a NULL pointer)? Or calling a function with an incorrect type, which will fail? (it is worth building your source files with -Werror (warnings are errors), as warnings can indicate undefined behavior which can cause this)
Build with ASSERTIONS=2 for more info.
''') # some useful text
    test(['-O1', '-s', 'ASSERTIONS=2'], ('''Invalid function pointer '0' called with signature 'v'. Perhaps this is an invalid value (e.g. caused by calling a virtual method on a NULL pointer)? Or calling a function with an incorrect type, which will fail? (it is worth building your source files with -Werror (warnings are errors), as warnings can indicate undefined behavior which can cause this)
This pointer might make sense in another type signature:''', '''Invalid function pointer '1' called with signature 'v'. Perhaps this is an invalid value (e.g. caused by calling a virtual method on a NULL pointer)? Or calling a function with an incorrect type, which will fail? (it is worth building your source files with -Werror (warnings are errors), as warnings can indicate undefined behavior which can cause this)'''), "i: asm['_my_func']") # actually useful identity of the bad pointer, with comparisons to what it would be in other types/tables
    test(['-O1', '-s', 'EMULATE_FUNCTION_POINTER_CASTS=1'], '''my func\n''') # emulate so it works

  def test_emulate_function_pointer_casts_assertions_2(self):
    # check empty tables work with assertions 2 in this mode (#6554)
    run_process([PYTHON, EMCC, path_from_root('tests', 'hello_world.c'), '-s', 'EMULATED_FUNCTION_POINTERS=1', '-s', 'ASSERTIONS=2'])

  def test_l_link(self):
    # Linking with -lLIBNAME and -L/DIRNAME should work, also should work with spaces

    def build(path, args):
        check_execute([PYTHON, EMCC, self.in_dir(*path)] + args)

    open(self.in_dir('main.cpp'), 'w').write('''
      extern void printey();
      int main() {
        printey();
        return 0;
      }
    ''')

    try:
      os.makedirs(self.in_dir('libdir'))
    except:
      pass

    open(self.in_dir('libdir', 'libfile.cpp'), 'w').write('''
      #include <stdio.h>
      void printey() {
        printf("hello from lib\\n");
      }
    ''')

    libfile = self.in_dir('libdir', 'libfile.so')
    aout = self.in_dir('a.out.js')

    # Test linking the library built here by emcc
    build(['libdir', 'libfile.cpp'], ['-c'])
    shutil.move(self.in_dir('libfile.o'), libfile)
    build(['main.cpp'], ['-L' + self.in_dir('libdir'), '-lfile'])

    self.assertContained('hello from lib', run_js(aout))

    # Also test execution with `-l c` and space-separated library linking syntax
    os.remove(aout)
    build(['libdir', 'libfile.cpp'], ['-c', '-l', 'c'])
    shutil.move(self.in_dir('libfile.o'), libfile)
    build(['main.cpp'], ['-L', self.in_dir('libdir'), '-l', 'file'])

    self.assertContained('hello from lib', run_js(aout))

    assert not os.path.exists('a.out') and not os.path.exists('a.exe'), 'Must not leave unneeded linker stubs'

  def test_commons_link(self):
    open('a.h', 'w').write(r'''
#if !defined(A_H)
#define A_H
extern int foo[8];
#endif
''')
    open('a.c', 'w').write(r'''
#include "a.h"
int foo[8];
''')
    open('main.c', 'w').write(r'''
#include <stdio.h>
#include "a.h"

int main() {
    printf("|%d|\n", foo[0]);
    return 0;
}
''')

    subprocess.check_call([PYTHON, EMCC, '-o', 'a.o', 'a.c'])
    subprocess.check_call([PYTHON, EMAR, 'rv', 'library.a', 'a.o'])
    subprocess.check_call([PYTHON, EMCC, '-o', 'main.o', 'main.c'])
    subprocess.check_call([PYTHON, EMCC, '-o', 'a.js', 'main.o', 'library.a', '-s', 'ERROR_ON_UNDEFINED_SYMBOLS=1'])
    self.assertContained('|0|', run_js('a.js'))

  def test_outline(self):
    if WINDOWS and not Building.which('mingw32-make'):
      self.skipTest('Skipping other.test_outline: This test requires "mingw32-make" tool in PATH on Windows to drive a Makefile build of zlib')

    def test(name, src, libs, expected, expected_ranges, args=[], suffix='cpp'):
      print(name)

      def measure_funcs(filename):
        i = 0
        start = -1
        curr = None
        ret = {}
        for line in open(filename):
          i += 1
          if line.startswith('function '):
            start = i
            curr = line
          elif line.startswith('}') and curr:
            size = i - start
            ret[curr] = size
            curr = None
        return ret

      for debug, outlining_limits in [
        ([], (1000,)),
        (['-g1'], (1000,)),
        (['-g2'], (1000,)),
        (['-g'], (100, 250, 500, 1000, 2000, 5000, 0))
      ]:
        for outlining_limit in outlining_limits:
          print('\n', Building.COMPILER_TEST_OPTS, debug, outlining_limit, '\n')
          # TODO: test without -g3, tell all sorts
          Popen([PYTHON, EMCC, src] + libs + ['-o', 'test.js', '-O2', '-s', 'WASM=0'] + debug + ['-s', 'OUTLINING_LIMIT=%d' % outlining_limit] + args).communicate()
          assert os.path.exists('test.js')
          shutil.copyfile('test.js', '%d_test.js' % outlining_limit)
          for engine in JS_ENGINES:
            if engine == V8_ENGINE: continue # ban v8, weird failures
            out = run_js('test.js', engine=engine, stderr=PIPE, full_output=True)
            self.assertContained(expected, out)
            if engine == SPIDERMONKEY_ENGINE: self.validate_asmjs(out)
          if debug == ['-g']:
            low = expected_ranges[outlining_limit][0]
            seen = max(measure_funcs('test.js').values())
            high = expected_ranges[outlining_limit][1]
            print(Building.COMPILER_TEST_OPTS, outlining_limit, '   ', low, '<=', seen, '<=', high)
            assert low <= seen <= high

    for test_opts, expected_ranges in [
      ([], {
         100: (150, 500),
         250: (150, 800),
         500: (150, 900),
        1000: (200, 1000),
        2000: (250, 2000),
        5000: (500, 5000),
           0: (1000, 5000)
      }),
      (['-O2'], {
         100: (0, 1600),
         250: (0, 1600),
         500: (0, 1600),
        1000: (0, 1600),
        2000: (0, 2000),
        5000: (0, 5000),
           0: (0, 5000)
      }),
    ]:
      Building.COMPILER_TEST_OPTS = test_opts
      test('zlib', path_from_root('tests', 'zlib', 'example.c'),
                   get_zlib_library(self),
                   open(path_from_root('tests', 'zlib', 'ref.txt'), 'r').read(),
                   expected_ranges,
                   args=['-I' + path_from_root('tests', 'zlib')], suffix='c')

  def test_outline_stack(self):
    open('src.c', 'w').write(r'''
#include <stdio.h>
#include <stdlib.h>

void *p = NULL;

void foo() {
  int * x = alloca(1);
};

int main() {
  printf("Hello, world!\n");
  for (int i=0; i<100000; i++) {
    free(p);
    foo();
  }
}
''')
    for limit in [0, 1000, 2500, 5000]:
      print(limit)
      subprocess.check_call([PYTHON, EMCC, 'src.c', '-s', 'ASSERTIONS=2', '-s', 'OUTLINING_LIMIT=%d' % limit, '-s', 'TOTAL_STACK=10000'])
      assert 'Hello, world!' in run_js('a.out.js')

  def test_symlink(self):
    self.clear()
    if os.name == 'nt':
      self.skipTest('Windows FS does not need to be tested for symlinks support, since it does not have them.')
    open(os.path.join(self.get_dir(), 'foobar.xxx'), 'w').write('int main(){ return 0; }')
    os.symlink(os.path.join(self.get_dir(), 'foobar.xxx'), os.path.join(self.get_dir(), 'foobar.c'))
    Popen([PYTHON, EMCC, os.path.join(self.get_dir(), 'foobar.c'), '-o', os.path.join(self.get_dir(), 'foobar')]).communicate()
    assert os.path.exists(os.path.join(self.get_dir(), 'foobar'))
    try_delete(os.path.join(self.get_dir(), 'foobar'))
    try_delete(os.path.join(self.get_dir(), 'foobar.xxx'))
    try_delete(os.path.join(self.get_dir(), 'foobar.c'))

    open(os.path.join(self.get_dir(), 'foobar.c'), 'w').write('int main(){ return 0; }')
    os.symlink(os.path.join(self.get_dir(), 'foobar.c'), os.path.join(self.get_dir(), 'foobar.xxx'))
    Popen([PYTHON, EMCC, os.path.join(self.get_dir(), 'foobar.xxx'), '-o', os.path.join(self.get_dir(), 'foobar')]).communicate()
    assert os.path.exists(os.path.join(self.get_dir(), 'foobar'))
    try_delete(os.path.join(self.get_dir(), 'foobar'))
    try_delete(os.path.join(self.get_dir(), 'foobar.xxx'))
    try_delete(os.path.join(self.get_dir(), 'foobar.c'))

  def test_multiply_defined_libsymbols(self):
    lib = "int mult() { return 1; }"
    lib_name = os.path.join(self.get_dir(), 'libA.c')
    open(lib_name, 'w').write(lib)
    a2 = "void x() {}"
    a2_name = os.path.join(self.get_dir(), 'a2.c')
    open(a2_name, 'w').write(a2)
    b2 = "void y() {}"
    b2_name = os.path.join(self.get_dir(), 'b2.c')
    open(b2_name, 'w').write(b2)
    main = r'''
      #include <stdio.h>
      int mult();
      int main() {
        printf("result: %d\n", mult());
        return 0;
      }
    '''
    main_name = os.path.join(self.get_dir(), 'main.c')
    open(main_name, 'w').write(main)

    Building.emcc(lib_name, output_filename='libA.so')

    Building.emcc(a2_name, ['-L.', '-lA'])
    Building.emcc(b2_name, ['-L.', '-lA'])

    Building.emcc(main_name, ['-L.', '-lA', a2_name+'.o', b2_name+'.o'], output_filename='a.out.js')

    self.assertContained('result: 1', run_js(os.path.join(self.get_dir(), 'a.out.js')))

  def test_multiply_defined_libsymbols_2(self):
    a = "int x() { return 55; }"
    a_name = os.path.join(self.get_dir(), 'a.c')
    open(a_name, 'w').write(a)
    b = "int y() { return 2; }"
    b_name = os.path.join(self.get_dir(), 'b.c')
    open(b_name, 'w').write(b)
    c = "int z() { return 5; }"
    c_name = os.path.join(self.get_dir(), 'c.c')
    open(c_name, 'w').write(c)
    main = r'''
      #include <stdio.h>
      int x();
      int y();
      int z();
      int main() {
        printf("result: %d\n", x() + y() + z());
        return 0;
      }
    '''
    main_name = os.path.join(self.get_dir(), 'main.c')
    open(main_name, 'w').write(main)

    Building.emcc(a_name) # a.c.o
    Building.emcc(b_name) # b.c.o
    Building.emcc(c_name) # c.c.o
    lib_name = os.path.join(self.get_dir(), 'libLIB.a')
    Building.emar('cr', lib_name, [a_name + '.o', b_name + '.o']) # libLIB.a with a and b

    # a is in the lib AND in an .o, so should be ignored in the lib. We do still need b from the lib though
    Building.emcc(main_name, [a_name+'.o', c_name + '.o', '-L.', '-lLIB'], output_filename='a.out.js')

    self.assertContained('result: 62', run_js(os.path.join(self.get_dir(), 'a.out.js')))

  def test_link_group(self):
    lib_src_name = os.path.join(self.get_dir(), 'lib.c')
    open(lib_src_name, 'w').write('int x() { return 42; }')

    main_name = os.path.join(self.get_dir(), 'main.c')
    open(main_name, 'w').write(r'''
      #include <stdio.h>
      int x();
      int main() {
        printf("result: %d\n", x());
        return 0;
      }
    ''')

    Building.emcc(lib_src_name) # lib.c.o
    lib_name = os.path.join(self.get_dir(), 'libLIB.a')
    Building.emar('cr', lib_name, [lib_src_name + '.o']) # libLIB.a with lib.c.o

    def test(lib_args, err_expected):
      print(err_expected)
      output = run_process([PYTHON, EMCC, main_name, '-o', 'a.out.js'] + lib_args, stdout=PIPE, stderr=PIPE, check=not err_expected)
      #print output.stderr
      if err_expected:
        self.assertContained(err_expected, output.stderr)
      else:
        self.assertNotContained('unresolved symbol', output.stderr)
        out_js = os.path.join(self.get_dir(), 'a.out.js')
        assert os.path.exists(out_js), output.stdout + '\n' + output.stderr
        self.assertContained('result: 42', run_js(out_js))

    test(['-Wl,--start-group', lib_name, '-Wl,--start-group'], 'Nested --start-group, missing --end-group?')
    test(['-Wl,--end-group', lib_name, '-Wl,--start-group'], '--end-group without --start-group')
    test(['-Wl,--start-group', lib_name, '-Wl,--end-group'], None)
    test(['-Wl,--start-group', lib_name], None)

    print('embind test with groups')

    main_name = os.path.join(self.get_dir(), 'main.cpp')
    open(main_name, 'w').write(r'''
      #include <stdio.h>
      #include <emscripten/val.h>
      using namespace emscripten;
      extern "C" int x();
      int main() {
        int y = -x();
        y = val::global("Math").call<int>("abs", y);
        printf("result: %d\n", y);
        return 0;
      }
    ''')
    test(['-Wl,--start-group', lib_name, '-Wl,--end-group', '--bind'], None)

  def test_whole_archive(self):
    # Verify that -Wl,--whole-archive includes the static constructor from the
    # otherwise unreferenced library.
    run_process([PYTHON, EMCC, '-c', '-o', 'main.o', path_from_root('tests', 'test_whole_archive', 'main.c')])
    run_process([PYTHON, EMCC, '-c', '-o', 'testlib.o', path_from_root('tests', 'test_whole_archive', 'testlib.c')])
    run_process([PYTHON, EMAR, 'crs', 'libtest.a', 'testlib.o'])

    run_process([PYTHON, EMCC, '-Wl,--whole-archive', 'libtest.a', '-Wl,--no-whole-archive', 'main.o'])
    self.assertContained('foo is: 42\n', run_js('a.out.js'))

    run_process([PYTHON, EMCC, '-Wl,-whole-archive', 'libtest.a', '-Wl,-no-whole-archive', 'main.o'])
    self.assertContained('foo is: 42\n', run_js('a.out.js'))

    # Verify the --no-whole-archive prevents the inclusion of the ctor
    run_process([PYTHON, EMCC, '-Wl,-whole-archive', '-Wl,--no-whole-archive', 'libtest.a', 'main.o'])
    self.assertContained('foo is: 0\n', run_js('a.out.js'))

  def test_link_group_bitcode(self):
    one = open('1.c', 'w').write(r'''
int f(void);
int main() {
  f();
  return 0;
}
    ''')
    two = open('2.c', 'w').write(r'''
#include <stdio.h>
int f() {
  printf("Hello\n");
  return 0;
}
    ''')

    Popen([PYTHON, EMCC, '-o', '1.o', '1.c']).communicate()
    Popen([PYTHON, EMCC, '-o', '2.o', '2.c']).communicate()
    Popen([PYTHON, EMAR, 'crs', '2.a', '2.o']).communicate()
    Popen([PYTHON, EMCC, '-o', 'out.bc', '-Wl,--start-group', '2.a', '1.o', '-Wl,--end-group']).communicate()
    Popen([PYTHON, EMCC, 'out.bc']).communicate()
    self.assertContained('Hello', run_js('a.out.js'))

  def test_circular_libs(self):
    def tmp_source(name, code):
      file_name = os.path.join(self.get_dir(), name)
      open(file_name, 'w').write(code)
      return file_name

    a = tmp_source('a.c', 'int z(); int x() { return z(); }')
    b = tmp_source('b.c', 'int x(); int y() { return x(); } int z() { return 42; }')
    c = tmp_source('c.c', 'int q() { return 0; }')
    main = tmp_source('main.c', r'''
      #include <stdio.h>
      int y();
      int main() {
        printf("result: %d\n", y());
        return 0;
      }
    ''')

    Building.emcc(a) # a.c.o
    Building.emcc(b) # b.c.o
    Building.emcc(c) # c.c.o
    lib_a = os.path.join(self.get_dir(), 'libA.a')
    Building.emar('cr', lib_a, [a + '.o', c + '.o']) # libA.a with a.c.o,c.c.o
    lib_b = os.path.join(self.get_dir(), 'libB.a')
    Building.emar('cr', lib_b, [b + '.o', c + '.o']) # libB.a with b.c.o,c.c.o

    args = ['-s', 'ERROR_ON_UNDEFINED_SYMBOLS=1', main, '-o', 'a.out.js']
    libs_list = [lib_a, lib_b]

    # lib_a does not satisfy any symbols from main, so it will not be included,
    # and there will be an unresolved symbol.
    output = run_process([PYTHON, EMCC] + args + libs_list, stdout=PIPE, stderr=PIPE, check=False)
    self.assertContained('error: unresolved symbol: x', output.stderr)

    # -Wl,--start-group and -Wl,--end-group around the libs will cause a rescan
    # of lib_a after lib_b adds undefined symbol "x", so a.c.o will now be
    # included (and the link will succeed).
    libs = ['-Wl,--start-group'] + libs_list + ['-Wl,--end-group']
    output = run_process([PYTHON, EMCC] + args + libs, stdout=PIPE, stderr=PIPE)
    out_js = os.path.join(self.get_dir(), 'a.out.js')
    assert os.path.exists(out_js), output.stdout + '\n' + output.stderr
    self.assertContained('result: 42', run_js(out_js))

    # -( and -) should also work.
    args = ['-s', 'ERROR_ON_UNDEFINED_SYMBOLS=1', main, '-o', 'a2.out.js']
    libs = ['-Wl,-('] + libs_list + ['-Wl,-)']
    output = run_process([PYTHON, EMCC] + args + libs, stdout=PIPE, stderr=PIPE)
    out_js = os.path.join(self.get_dir(), 'a2.out.js')
    assert os.path.exists(out_js), output.stdout + '\n' + output.stderr
    self.assertContained('result: 42', run_js(out_js))

  def test_redundant_link(self):
    lib = "int mult() { return 1; }"
    lib_name = os.path.join(self.get_dir(), 'libA.c')
    open(lib_name, 'w').write(lib)
    main = r'''
      #include <stdio.h>
      int mult();
      int main() {
        printf("result: %d\n", mult());
        return 0;
      }
    '''
    main_name = os.path.join(self.get_dir(), 'main.c')
    open(main_name, 'w').write(main)

    Building.emcc(lib_name, output_filename='libA.so')

    Building.emcc(main_name, ['libA.so']*2, output_filename='a.out.js')

    self.assertContained('result: 1', run_js(os.path.join(self.get_dir(), 'a.out.js')))

  def test_dot_a_all_contents_invalid(self):
    # check that we warn if an object file in a .a is not valid bitcode.
    # do not silently ignore native object files, which may have been
    # built by mistake
    open('side.cpp', 'w').write(r'''int side() { return 5; }''')
    open('main.cpp', 'w').write(r'''extern int side(); int main() { return side(); }''')
    run_process([CLANG, 'side.cpp', '-c', '-o', 'native.o'])
    run_process([PYTHON, EMAR, 'crs', 'foo.a', 'native.o'])
    err = run_process([PYTHON, EMCC, 'main.cpp', 'foo.a'], stderr=PIPE).stderr
    self.assertContained('warning: unresolved symbol: _Z4sidev', err) # was native, could not link it
    self.assertContained('is not LLVM bitcode, cannot link', err)

  def test_export_all(self):
    lib = r'''
      #include <stdio.h>
      void libf1() { printf("libf1\n"); }
      void libf2() { printf("libf2\n"); }
    '''
    lib_name = os.path.join(self.get_dir(), 'lib.c')
    open(lib_name, 'w').write(lib)

    open('main.js', 'w').write('''
      var Module = {
        onRuntimeInitialized: function() {
          _libf1();
          _libf2();
        }
      };
    ''')

    Building.emcc(lib_name, ['-s', 'EXPORT_ALL=1', '-s', 'LINKABLE=1', '--pre-js', 'main.js'], output_filename='a.out.js')

    self.assertContained('libf1\nlibf2\n', run_js(os.path.join(self.get_dir(), 'a.out.js')))

  def test_stdin(self):
    def _test():
      for engine in JS_ENGINES:
        if engine == V8_ENGINE: continue # no stdin support in v8 shell
        engine[0] = os.path.normpath(engine[0])
        print(engine, file=sys.stderr)
        # work around a bug in python's subprocess module
        # (we'd use run_js() normally)
        try_delete('out.txt')
        if os.name == 'nt': # windows
          os.system('type "in.txt" | {} >out.txt'.format(' '.join(Building.doublequote_spaces(make_js_command(os.path.normpath(exe), engine)))))
        else: # posix
          os.system('cat in.txt | {} > out.txt'.format(' '.join(Building.doublequote_spaces(make_js_command(exe, engine)))))
        self.assertContained('abcdef\nghijkl\neof', open('out.txt').read())

    Building.emcc(path_from_root('tests', 'module', 'test_stdin.c'), output_filename='a.out.js')
    open('in.txt', 'w').write('abcdef\nghijkl')
    exe = os.path.join(self.get_dir(), 'a.out.js')
    _test()
    Building.emcc(path_from_root('tests', 'module', 'test_stdin.c'),
                  ['-O2', '--closure', '1'],
                  output_filename='a.out.js')
    _test()

  def test_ungetc_fscanf(self):
    open('main.cpp', 'w').write(r'''
      #include <stdio.h>
      int main(int argc, char const *argv[])
      {
          char str[4] = {0};
          FILE* f = fopen("my_test.input", "r");
          if (f == NULL) {
              printf("cannot open file\n");
              return -1;
          }
          ungetc('x', f);
          ungetc('y', f);
          ungetc('z', f);
          fscanf(f, "%3s", str);
          printf("%s\n", str);
          return 0;
      }
    ''')
    open('my_test.input', 'w').write('abc')
    Building.emcc('main.cpp', ['--embed-file', 'my_test.input'], output_filename='a.out.js')
    self.assertContained('zyx', run_process(JS_ENGINES[0] + ['a.out.js'], stdout=PIPE, stderr=PIPE).stdout)

  def test_abspaths(self):
    # Includes with absolute paths are generally dangerous, things like -I/usr/.. will get to system local headers, not our portable ones.

    shutil.copyfile(path_from_root('tests', 'hello_world.c'), 'main.c')

    for args, expected in [(['-I/usr/something', '-Wwarn-absolute-paths'], True),
                           (['-L/usr/something', '-Wwarn-absolute-paths'], True),
                           (['-I/usr/something'], False),
                           (['-L/usr/something'], False),
                           (['-I/usr/something', '-Wno-warn-absolute-paths'], False),
                           (['-L/usr/something', '-Wno-warn-absolute-paths'], False),
                           (['-Isubdir/something', '-Wwarn-absolute-paths'], False),
                           (['-Lsubdir/something', '-Wwarn-absolute-paths'], False),
                           ([], False)]:
      print(args, expected)
      proc = run_process([PYTHON, EMCC, 'main.c'] + args, stderr=PIPE)
      assert ('encountered. If this is to a local system header/library, it may cause problems (local system files make sense for compiling natively on your system, but not necessarily to JavaScript)' in proc.stderr) == expected, proc.stderr
      if not expected:
        assert proc.stderr == '', proc.stderr

  def test_local_link(self):
    # Linking a local library directly, like /usr/lib/libsomething.so, cannot work of course since it
    # doesn't contain bitcode. However, when we see that we should look for a bitcode file for that
    # library in the -L paths and system/lib
    open(os.path.join(self.get_dir(), 'main.cpp'), 'w').write('''
      extern void printey();
      int main() {
        printey();
        return 0;
      }
    ''')

    try:
      os.makedirs(os.path.join(self.get_dir(), 'subdir'));
    except:
      pass
    open(os.path.join(self.get_dir(), 'subdir', 'libfile.so'), 'w').write('this is not llvm bitcode!')

    open(os.path.join(self.get_dir(), 'libfile.cpp'), 'w').write('''
      #include <stdio.h>
      void printey() {
        printf("hello from lib\\n");
      }
    ''')

    Popen([PYTHON, EMCC, os.path.join(self.get_dir(), 'libfile.cpp'), '-o', 'libfile.so']).communicate()
    Popen([PYTHON, EMCC, os.path.join(self.get_dir(), 'main.cpp'), os.path.join(self.get_dir(), 'subdir', 'libfile.so'), '-L.'], stderr=PIPE).communicate()
    self.assertContained('hello from lib', run_js(os.path.join(self.get_dir(), 'a.out.js')))

  def test_identical_basenames(self):
    # Issue 287: files in different dirs but with the same basename get confused as the same,
    # causing multiply defined symbol errors
    try:
      os.makedirs(os.path.join(self.get_dir(), 'foo'));
    except:
      pass
    try:
      os.makedirs(os.path.join(self.get_dir(), 'bar'));
    except:
      pass
    open(os.path.join(self.get_dir(), 'foo', 'main.cpp'), 'w').write('''
      extern void printey();
      int main() {
        printey();
        return 0;
      }
    ''')
    open(os.path.join(self.get_dir(), 'bar', 'main.cpp'), 'w').write('''
      #include <stdio.h>
      void printey() { printf("hello there\\n"); }
    ''')

    Popen([PYTHON, EMCC, os.path.join(self.get_dir(), 'foo', 'main.cpp'), os.path.join(self.get_dir(), 'bar', 'main.cpp')]).communicate()
    self.assertContained('hello there', run_js(os.path.join(self.get_dir(), 'a.out.js')))

    # ditto with first creating .o files
    try_delete(os.path.join(self.get_dir(), 'a.out.js'))
    Popen([PYTHON, EMCC, os.path.join(self.get_dir(), 'foo', 'main.cpp'), '-o', os.path.join(self.get_dir(), 'foo', 'main.o')]).communicate()
    Popen([PYTHON, EMCC, os.path.join(self.get_dir(), 'bar', 'main.cpp'), '-o', os.path.join(self.get_dir(), 'bar', 'main.o')]).communicate()
    Popen([PYTHON, EMCC, os.path.join(self.get_dir(), 'foo', 'main.o'), os.path.join(self.get_dir(), 'bar', 'main.o')]).communicate()
    self.assertContained('hello there', run_js(os.path.join(self.get_dir(), 'a.out.js')))

  def test_main_a(self):
    # if main() is in a .a, we need to pull in that .a

    main_name = os.path.join(self.get_dir(), 'main.c')
    open(main_name, 'w').write(r'''
      #include <stdio.h>
      extern int f();
      int main() {
        printf("result: %d.\n", f());
        return 0;
      }
    ''')

    other_name = os.path.join(self.get_dir(), 'other.c')
    open(other_name, 'w').write(r'''
      #include <stdio.h>
      int f() { return 12346; }
    ''')

    Popen([PYTHON, EMCC, main_name, '-c', '-o', main_name+'.bc']).communicate()
    Popen([PYTHON, EMCC, other_name, '-c', '-o', other_name+'.bc']).communicate()

    Popen([PYTHON, EMAR, 'cr', main_name+'.a', main_name+'.bc']).communicate()

    Popen([PYTHON, EMCC, other_name+'.bc', main_name+'.a']).communicate()

    self.assertContained('result: 12346.', run_js(os.path.join(self.get_dir(), 'a.out.js')))

  def test_dup_o_in_a(self):
    open('common.c', 'w').write(r'''
      #include <stdio.h>
      void a(void) {
        printf("a\n");
      }
    ''')
    Popen([PYTHON, EMCC, 'common.c', '-c', '-o', 'common.o']).communicate()
    Popen([PYTHON, EMAR, 'rc', 'liba.a', 'common.o']).communicate()

    open('common.c', 'w').write(r'''
      #include <stdio.h>
      void b(void) {
        printf("b\n");
      }
    ''')
    Popen([PYTHON, EMCC, 'common.c', '-c', '-o', 'common.o']).communicate()
    Popen([PYTHON, EMAR, 'rc', 'libb.a', 'common.o']).communicate()

    open('main.c', 'w').write(r'''
      void a(void);
      void b(void);
      int main() {
        a();
        b();
      }
    ''')
    Popen([PYTHON, EMCC, 'main.c', '-L.', '-la', '-lb']).communicate()

    self.assertContained('a\nb\n', run_js(os.path.join(self.get_dir(), 'a.out.js')))

  def test_dup_o_in_one_a(self):
    open('common.c', 'w').write(r'''
      #include <stdio.h>
      void a(void) {
        printf("a\n");
      }
    ''')
    Popen([PYTHON, EMCC, 'common.c', '-c', '-o', 'common.o']).communicate()

    try:
      os.makedirs(os.path.join(self.get_dir(), 'libdir'));
    except:
      pass
    open(os.path.join('libdir', 'common.c'), 'w').write(r'''
      #include <stdio.h>
      void b(void) {
        printf("b...\n");
      }
    ''')
    Popen([PYTHON, EMCC, os.path.join('libdir', 'common.c'), '-c', '-o', os.path.join('libdir', 'common.o')]).communicate()

    Popen([PYTHON, EMAR, 'rc', 'liba.a', 'common.o', os.path.join('libdir', 'common.o')]).communicate()

    open('main.c', 'w').write(r'''
      void a(void);
      void b(void);
      int main() {
        a();
        b();
      }
    ''')
    err = run_process([PYTHON, EMCC, 'main.c', '-L.', '-la'], stderr=PIPE).stderr
    assert 'loading from archive' not in err, err
    assert 'which has duplicate entries' not in err, err
    assert 'duplicate: common.o' not in err, err
    self.assertContained('a\nb...\n', run_js('a.out.js'))

    text = run_process([PYTHON, EMAR, 't', 'liba.a'], stdout=PIPE).stdout
    assert 'common.o' not in text, text
    assert text.count('common_') == 2, text
    for line in text.split('\n'):
      assert len(line) < 20, line # should not have huge hash names

    # make the hashing fail: 'q' is just a quick append, no replacement, so hashing is not done, and dupes are easy
    Popen([PYTHON, EMAR, 'q', 'liba.a', 'common.o', os.path.join('libdir', 'common.o')]).communicate()
    err = run_process([PYTHON, EMCC, 'main.c', '-L.', '-la'], stderr=PIPE).stderr
    assert 'loading from archive' in err, err
    assert 'which has duplicate entries' in err, err
    assert 'duplicate: common.o' in err, err
    assert err.count('duplicate: ') == 1, err # others are not duplicates - the hashing keeps them separate

  def test_export_in_a(self):
    export_name = 'this_is_an_entry_point'
    full_export_name = '_' + export_name

    open('export.c', 'w').write(r'''
      #include <stdio.h>
      void %s(void) {
        printf("Hello, world!\n");
      }
    ''' % export_name)
    Popen([PYTHON, EMCC, 'export.c', '-c', '-o', 'export.o']).communicate()
    Popen([PYTHON, EMAR, 'rc', 'libexport.a', 'export.o']).communicate()

    open('main.c', 'w').write(r'''
      int main() {
        return 0;
      }
    ''')

    # Sanity check: the symbol should not be linked in if not requested.
    Popen([PYTHON, EMCC, 'main.c', '-L.', '-lexport']).communicate()
    assert not self.is_exported_in_wasm(full_export_name, 'a.out.wasm')

    # Sanity check: exporting without a definition does not cause it to appear.
    # Note: exporting main prevents emcc from warning that it generated no code.
    Popen([PYTHON, EMCC, 'main.c', '-s', '''EXPORTED_FUNCTIONS=['_main', '%s']''' % full_export_name]).communicate()
    assert not self.is_exported_in_wasm(full_export_name, 'a.out.wasm')

    # Actual test: defining symbol in library and exporting it causes it to appear in the output.
    Popen([PYTHON, EMCC, 'main.c', '-L.', '-lexport', '-s', '''EXPORTED_FUNCTIONS=['%s']''' % full_export_name]).communicate()
    assert self.is_exported_in_wasm(full_export_name, 'a.out.wasm')

  def test_embed_file(self):
    open(os.path.join(self.get_dir(), 'somefile.txt'), 'w').write('''hello from a file with lots of data and stuff in it thank you very much''')
    open(os.path.join(self.get_dir(), 'main.cpp'), 'w').write(r'''
      #include <stdio.h>
      int main() {
        FILE *f = fopen("somefile.txt", "r");
        char buf[100];
        fread(buf, 1, 20, f);
        buf[20] = 0;
        fclose(f);
        printf("|%s|\n", buf);
        return 0;
      }
    ''')

    Popen([PYTHON, EMCC, os.path.join(self.get_dir(), 'main.cpp'), '--embed-file', 'somefile.txt']).communicate()
    self.assertContained('|hello from a file wi|', run_js(os.path.join(self.get_dir(), 'a.out.js')))

    # preload twice, should not err
    Popen([PYTHON, EMCC, os.path.join(self.get_dir(), 'main.cpp'), '--embed-file', 'somefile.txt', '--embed-file', 'somefile.txt']).communicate()
    self.assertContained('|hello from a file wi|', run_js(os.path.join(self.get_dir(), 'a.out.js')))

  def test_embed_file_dup(self):
    try_delete(os.path.join(self.get_dir(), 'tst'))
    os.mkdir(os.path.join(self.get_dir(), 'tst'))
    os.mkdir(os.path.join(self.get_dir(), 'tst', 'test1'))
    os.mkdir(os.path.join(self.get_dir(), 'tst', 'test2'))

    open(os.path.join(self.get_dir(), 'tst', 'aa.txt'), 'w').write('''frist''')
    open(os.path.join(self.get_dir(), 'tst', 'test1', 'aa.txt'), 'w').write('''sacond''')
    open(os.path.join(self.get_dir(), 'tst', 'test2', 'aa.txt'), 'w').write('''thard''')
    open(os.path.join(self.get_dir(), 'main.cpp'), 'w').write(r'''
      #include <stdio.h>
      #include <string.h>
      void print_file(const char *name) {
        FILE *f = fopen(name, "r");
        char buf[100];
        memset(buf, 0, 100);
        fread(buf, 1, 20, f);
        buf[20] = 0;
        fclose(f);
        printf("|%s|\n", buf);
      }
      int main() {
        print_file("tst/aa.txt");
        print_file("tst/test1/aa.txt");
        print_file("tst/test2/aa.txt");
        return 0;
      }
    ''')

    Popen([PYTHON, EMCC, os.path.join(self.get_dir(), 'main.cpp'), '--embed-file', 'tst']).communicate()
    self.assertContained('|frist|\n|sacond|\n|thard|\n', run_js(os.path.join(self.get_dir(), 'a.out.js')))

  def test_exclude_file(self):
    try_delete(os.path.join(self.get_dir(), 'tst'))
    os.mkdir(os.path.join(self.get_dir(), 'tst'))
    os.mkdir(os.path.join(self.get_dir(), 'tst', 'abc.exe'))
    os.mkdir(os.path.join(self.get_dir(), 'tst', 'abc.txt'))

    open(os.path.join(self.get_dir(), 'tst', 'hello.exe'), 'w').write('''hello''')
    open(os.path.join(self.get_dir(), 'tst', 'hello.txt'), 'w').write('''world''')
    open(os.path.join(self.get_dir(), 'tst', 'abc.exe', 'foo'), 'w').write('''emscripten''')
    open(os.path.join(self.get_dir(), 'tst', 'abc.txt', 'bar'), 'w').write('''!!!''')
    open(os.path.join(self.get_dir(), 'main.cpp'), 'w').write(r'''
      #include <stdio.h>
      int main() {
        if(fopen("tst/hello.exe", "rb")) printf("Failed\n");
        if(!fopen("tst/hello.txt", "rb")) printf("Failed\n");
        if(fopen("tst/abc.exe/foo", "rb")) printf("Failed\n");
        if(!fopen("tst/abc.txt/bar", "rb")) printf("Failed\n");

        return 0;
      }
    ''')

    Popen([PYTHON, EMCC, os.path.join(self.get_dir(), 'main.cpp'), '--embed-file', 'tst', '--exclude-file', '*.exe']).communicate()
    output = run_js(os.path.join(self.get_dir(), 'a.out.js'))
    assert output == '' or output == ' \n'

  def test_multidynamic_link(self):
    # Linking the same dynamic library in statically will error, normally, since we statically link it, causing dupe symbols

    def test(link_cmd, lib_suffix=''):
      print(link_cmd, lib_suffix)

      self.clear()

      open(os.path.join(self.get_dir(), 'main.cpp'), 'w').write(r'''
        #include <stdio.h>
        extern void printey();
        extern void printother();
        int main() {
          printf("*");
          printey();
          printf("\n");
          printother();
          printf("\n");
          printf("*");
          return 0;
        }
      ''')

      try:
        os.makedirs(os.path.join(self.get_dir(), 'libdir'));
      except:
        pass

      open(os.path.join(self.get_dir(), 'libdir', 'libfile.cpp'), 'w').write('''
        #include <stdio.h>
        void printey() {
          printf("hello from lib");
        }
      ''')

      open(os.path.join(self.get_dir(), 'libdir', 'libother.cpp'), 'w').write('''
        #include <stdio.h>
        extern void printey();
        void printother() {
          printf("|");
          printey();
          printf("|");
        }
      ''')

      compiler = [PYTHON, EMCC]

      # Build libfile normally into an .so
      Popen(compiler + [os.path.join(self.get_dir(), 'libdir', 'libfile.cpp'), '-o', os.path.join(self.get_dir(), 'libdir', 'libfile.so' + lib_suffix)]).communicate()
      # Build libother and dynamically link it to libfile
      Popen(compiler + [os.path.join(self.get_dir(), 'libdir', 'libother.cpp')] + link_cmd + ['-o', os.path.join(self.get_dir(), 'libdir', 'libother.so')]).communicate()
      # Build the main file, linking in both the libs
      Popen(compiler + [os.path.join(self.get_dir(), 'main.cpp')] + link_cmd + ['-lother', '-c']).communicate()
      print('...')
      # The normal build system is over. We need to do an additional step to link in the dynamic libraries, since we ignored them before
      Popen([PYTHON, EMCC, os.path.join(self.get_dir(), 'main.o')] + link_cmd + ['-lother', '-s', 'NO_EXIT_RUNTIME=0']).communicate()

      self.assertContained('*hello from lib\n|hello from lib|\n*', run_js(os.path.join(self.get_dir(), 'a.out.js')))

    test(['-L' + os.path.join(self.get_dir(), 'libdir'), '-lfile']) # -l, auto detection from library path
    test(['-L' + os.path.join(self.get_dir(), 'libdir'), os.path.join(self.get_dir(), 'libdir', 'libfile.so.3.1.4.1.5.9')], '.3.1.4.1.5.9') # handle libX.so.1.2.3 as well

  def test_js_link(self):
    open(os.path.join(self.get_dir(), 'main.cpp'), 'w').write('''
      #include <stdio.h>
      int main() {
        printf("hello from main\\n");
        return 0;
      }
    ''')
    open(os.path.join(self.get_dir(), 'before.js'), 'w').write('''
      var MESSAGE = 'hello from js';
      // Module is initialized with empty object by default, so if there are no keys - nothing was run yet
      if (Object.keys(Module).length) throw 'This code should run before anything else!';
    ''')
    open(os.path.join(self.get_dir(), 'after.js'), 'w').write('''
      out(MESSAGE);
    ''')

    Popen([PYTHON, EMCC, os.path.join(self.get_dir(), 'main.cpp'), '--pre-js', 'before.js', '--post-js', 'after.js', '-s', 'BINARYEN_ASYNC_COMPILATION=0']).communicate()
    self.assertContained('hello from main\nhello from js\n', run_js(os.path.join(self.get_dir(), 'a.out.js')))

  def test_sdl_endianness(self):
    open(os.path.join(self.get_dir(), 'main.cpp'), 'w').write(r'''
      #include <stdio.h>
      #include <SDL/SDL.h>

      int main() {
        printf("%d, %d, %d\n", SDL_BYTEORDER, SDL_LIL_ENDIAN, SDL_BIG_ENDIAN);
        return 0;
      }
    ''')
    Popen([PYTHON, EMCC, os.path.join(self.get_dir(), 'main.cpp')]).communicate()
    self.assertContained('1234, 1234, 4321\n', run_js(os.path.join(self.get_dir(), 'a.out.js')))

  def test_libpng(self):
    shutil.copyfile(path_from_root('tests', 'pngtest.png'), 'pngtest.png')
    Building.emcc(path_from_root('tests','pngtest.c'), ['--embed-file', 'pngtest.png', '-s', 'USE_ZLIB=1', '-s', 'USE_LIBPNG=1'], output_filename='a.out.js')
    self.assertContained('TESTS PASSED', run_process(JS_ENGINES[0] + ['a.out.js'], stdout=PIPE, stderr=PIPE).stdout)

  def test_bullet(self):
    Building.emcc(path_from_root('tests','bullet_hello_world.cpp'), ['-s', 'USE_BULLET=1'], output_filename='a.out.js')
    self.assertContained('BULLET RUNNING', run_process(JS_ENGINES[0] + ['a.out.js'], stdout=PIPE, stderr=PIPE).stdout)

  def test_vorbis(self):
    #This will also test if ogg compiles, because vorbis depends on ogg
    Building.emcc(path_from_root('tests','vorbis_test.c'), ['-s', 'USE_VORBIS=1'], output_filename='a.out.js')
    self.assertContained('ALL OK', run_process(JS_ENGINES[0] + ['a.out.js'], stdout=PIPE, stderr=PIPE).stdout)

  def test_freetype(self):
    # copy the Liberation Sans Bold truetype file located in the <emscripten_root>/tests/freetype to the compilation folder
    shutil.copy2(path_from_root('tests/freetype','LiberationSansBold.ttf'), os.getcwd())
    # build test program with the font file embed in it
    Building.emcc(path_from_root('tests','freetype_test.c'), ['-s', 'USE_FREETYPE=1', '--embed-file', 'LiberationSansBold.ttf'], output_filename='a.out.js')
    # the test program will print an ascii representation of a bitmap where the 'w' character has been rendered using the Liberation Sans Bold font
    expectedOutput = '***   +***+   **\n' + \
                     '***+  +***+  +**\n' + \
                     '***+  *****  +**\n' + \
                     '+**+ +**+**+ +**\n' + \
                     '+*** +**+**+ ***\n' + \
                     ' *** +** **+ ***\n' + \
                     ' ***+**+ +**+**+\n' + \
                     ' +**+**+ +**+**+\n' + \
                     ' +*****  +*****+\n' + \
                     '  *****   ***** \n' + \
                     '  ****+   +***+ \n' + \
                     '  +***+   +***+ \n'
    self.assertContained(expectedOutput, run_process(JS_ENGINES[0] + ['a.out.js'], stdout=PIPE, stderr=PIPE).stdout)

  def test_link_memcpy(self):
    # memcpy can show up *after* optimizations, so after our opportunity to link in libc, so it must be special-cased
    open(os.path.join(self.get_dir(), 'main.cpp'), 'w').write(r'''
      #include <stdio.h>

      int main(int argc, char **argv) {
        int num = argc + 10;
        char buf[num], buf2[num];
        for (int i = 0; i < num; i++) {
          buf[i] = i*i+i/3;
        }
        for (int i = 1; i < num; i++) {
          buf[i] += buf[i-1];
        }
        for (int i = 0; i < num; i++) {
          buf2[i] = buf[i];
        }
        for (int i = 1; i < num; i++) {
          buf2[i] += buf2[i-1];
        }
        for (int i = 0; i < num; i++) {
          printf("%d:%d\n", i, buf2[i]);
        }
        return 0;
      }
    ''')
    Popen([PYTHON, EMCC, '-O2', os.path.join(self.get_dir(), 'main.cpp')]).communicate()
    output = run_js(os.path.join(self.get_dir(), 'a.out.js'), full_output=True, stderr=PIPE)
    self.assertContained('''0:0
1:1
2:6
3:21
4:53
5:111
6:-49
7:98
8:55
9:96
10:-16
''', output)
    self.assertNotContained('warning: library.js memcpy should not be running, it is only for testing!', output)

  def test_warn_undefined(self):
    open(os.path.join(self.get_dir(), 'main.cpp'), 'w').write(r'''
      #include <stdio.h>
      #include <SDL.h>
      #include "SDL/SDL_opengl.h"

      extern "C" {
        void something();
        void elsey();
      }

      int main() {
        printf("%p", SDL_GL_GetProcAddress("glGenTextures")); // pull in gl proc stuff, avoid warnings on emulation funcs
        something();
        elsey();
        return 0;
      }
    ''')

    def clear(): try_delete('a.out.js')

    for args in [[], ['-O2']]:
      for action in ['WARN', 'ERROR', None]:
        for value in ([0, 1] if action else [0]):
          clear()
          print('warn', args, action, value)
          extra = ['-s', action + '_ON_UNDEFINED_SYMBOLS=%d' % value] if action else []
          output = run_process([PYTHON, EMCC, os.path.join(self.get_dir(), 'main.cpp')] + extra + args, stderr=PIPE, check=False)
          if action == None or (action == 'WARN' and value):
            self.assertContained('unresolved symbol: something', output.stderr)
            self.assertContained('unresolved symbol: elsey', output.stderr)
            assert os.path.exists('a.out.js')
            self.assertNotContained('unresolved symbol: emscripten_', output.stderr)
          elif action == 'ERROR' and value:
            self.assertContained('unresolved symbol: something', output.stderr)
            self.assertContained('unresolved symbol: elsey', output.stderr)
            self.assertNotContained('warning', output.stderr)
            assert not os.path.exists('a.out.js')
          elif action == 'WARN' and not value:
            self.assertNotContained('unresolved symbol', output.stderr)
            assert os.path.exists('a.out.js')

  def test_toobig(self):
    # very large [N x i8], we should not oom in the compiler
    self.clear()
    open(os.path.join(self.get_dir(), 'main.cpp'), 'w').write(r'''
      #include <stdio.h>

      #define BYTES 100*1024*1024

      int main(int argc, char **argv) {
        if (argc == 100) {
          static char buf[BYTES];
          static char buf2[BYTES];
          for (int i = 0; i < BYTES; i++) {
            buf[i] = i*i;
            buf2[i] = i/3;
          }
          for (int i = 0; i < BYTES; i++) {
            buf[i] = buf2[i/2];
            buf2[i] = buf[i/3];
          }
          printf("%d\n", buf[10] + buf2[20]);
        }
        return 0;
      }
    ''')
    output = run_process([PYTHON, EMCC, os.path.join(self.get_dir(), 'main.cpp')], stderr=PIPE).stderr
    print(output)
    assert os.path.exists('a.out.js')

  def test_prepost(self):
    open(os.path.join(self.get_dir(), 'main.cpp'), 'w').write('''
      #include <stdio.h>
      int main() {
        printf("hello from main\\n");
        return 0;
      }
    ''')
    open(os.path.join(self.get_dir(), 'pre.js'), 'w').write('''
      var Module = {
        preRun: function() { out('pre-run') },
        postRun: function() { out('post-run') }
      };
    ''')

    run_process([PYTHON, EMCC, os.path.join(self.get_dir(), 'main.cpp'), '--pre-js', 'pre.js', '-s', 'BINARYEN_ASYNC_COMPILATION=0'])
    self.assertContained('pre-run\nhello from main\npost-run\n', run_js(os.path.join(self.get_dir(), 'a.out.js')))

    # never run, so no preRun or postRun
    src = open(os.path.join(self.get_dir(), 'a.out.js')).read().replace('// {{PRE_RUN_ADDITIONS}}', 'addRunDependency()')
    open(os.path.join(self.get_dir(), 'a.out.js'), 'w').write(src)
    self.assertNotContained('pre-run\nhello from main\npost-run\n', run_js(os.path.join(self.get_dir(), 'a.out.js')))

    # noInitialRun prevents run
    for no_initial_run, run_dep in [(0, 0), (1, 0), (0, 1)]:
      print(no_initial_run, run_dep)
      args = ['-s', 'BINARYEN_ASYNC_COMPILATION=0']
      if no_initial_run:
        args += ['-s', 'INVOKE_RUN=0']
      if run_dep:
        with open('pre.js', 'w') as f:
          f.write('Module.preRun = function() { addRunDependency("test"); }')
        with open('post.js', 'w') as f:
          f.write('removeRunDependency("test");')
        args += ['--pre-js', 'pre.js', '--post-js', 'post.js']

      run_process([PYTHON, EMCC, os.path.join(self.get_dir(), 'main.cpp')] +  args)

      output = run_js(os.path.join(self.get_dir(), 'a.out.js'))
      if no_initial_run:
        self.assertNotContained('hello from main', output)
      else:
        self.assertContained('hello from main', output)

      if no_initial_run:
        # Calling main later should still work, filesystem etc. must be set up.
        print('call main later')
        src = open(os.path.join(self.get_dir(), 'a.out.js')).read() + '\nModule.callMain();\n';
        open(os.path.join(self.get_dir(), 'a.out.js'), 'w').write(src)
        self.assertContained('hello from main', run_js(os.path.join(self.get_dir(), 'a.out.js')))

    # Use postInit
    open(os.path.join(self.get_dir(), 'pre.js'), 'w').write('''
      var Module = {
        preRun: function() { out('pre-run') },
        postRun: function() { out('post-run') },
        preInit: function() { out('pre-init') }
      };
    ''')
    Popen([PYTHON, EMCC, os.path.join(self.get_dir(), 'main.cpp'), '--pre-js', 'pre.js']).communicate()
    self.assertContained('pre-init\npre-run\nhello from main\npost-run\n', run_js(os.path.join(self.get_dir(), 'a.out.js')))

  def test_prepost2(self):
    open(os.path.join(self.get_dir(), 'main.cpp'), 'w').write('''
      #include <stdio.h>
      int main() {
        printf("hello from main\\n");
        return 0;
      }
    ''')
    open(os.path.join(self.get_dir(), 'pre.js'), 'w').write('''
      var Module = {
        preRun: function() { out('pre-run') },
      };
    ''')
    open(os.path.join(self.get_dir(), 'pre2.js'), 'w').write('''
      Module.postRun = function() { out('post-run') };
    ''')
    Popen([PYTHON, EMCC, os.path.join(self.get_dir(), 'main.cpp'), '--pre-js', 'pre.js', '--pre-js', 'pre2.js']).communicate()
    self.assertContained('pre-run\nhello from main\npost-run\n', run_js(os.path.join(self.get_dir(), 'a.out.js')))

  def test_prepre(self):
    open(os.path.join(self.get_dir(), 'main.cpp'), 'w').write('''
      #include <stdio.h>
      int main() {
        printf("hello from main\\n");
        return 0;
      }
    ''')
    open(os.path.join(self.get_dir(), 'pre.js'), 'w').write('''
      var Module = {
        preRun: [function() { out('pre-run') }],
      };
    ''')
    open(os.path.join(self.get_dir(), 'pre2.js'), 'w').write('''
      Module.preRun.push(function() { out('prepre') });
    ''')
    Popen([PYTHON, EMCC, os.path.join(self.get_dir(), 'main.cpp'), '--pre-js', 'pre.js', '--pre-js', 'pre2.js']).communicate()
    self.assertContained('prepre\npre-run\nhello from main\n', run_js(os.path.join(self.get_dir(), 'a.out.js')))

  def test_save_bc(self):
    for save in [0, 1]:
      self.clear()
      Popen([PYTHON, EMCC, path_from_root('tests', 'hello_world_loop_malloc.cpp')] + ([] if not save else ['--save-bc', self.in_dir('my_bitcode.bc')])).communicate()
      assert 'hello, world!' in run_js(self.in_dir('a.out.js'))
      assert os.path.exists(self.in_dir('my_bitcode.bc')) == save
      if save:
        try_delete('a.out.js')
        Building.llvm_dis(self.in_dir('my_bitcode.bc'), self.in_dir('my_ll.ll'))
        try:
          os.environ['EMCC_LEAVE_INPUTS_RAW'] = '1'
          Popen([PYTHON, EMCC, 'my_ll.ll', '-o', 'two.js']).communicate()
          assert 'hello, world!' in run_js(self.in_dir('two.js'))
        finally:
          del os.environ['EMCC_LEAVE_INPUTS_RAW']

  def test_js_optimizer(self):
    for input, expected, passes in [
      (path_from_root('tests', 'optimizer', 'eliminateDeadGlobals.js'), open(path_from_root('tests', 'optimizer', 'eliminateDeadGlobals-output.js')).read(),
       ['eliminateDeadGlobals']),
      (path_from_root('tests', 'optimizer', 'test-js-optimizer.js'), open(path_from_root('tests', 'optimizer', 'test-js-optimizer-output.js')).read(),
       ['hoistMultiples', 'removeAssignsToUndefined', 'simplifyExpressions']),
      (path_from_root('tests', 'optimizer', 'test-js-optimizer-asm.js'), open(path_from_root('tests', 'optimizer', 'test-js-optimizer-asm-output.js')).read(),
       ['asm', 'simplifyExpressions']),
      (path_from_root('tests', 'optimizer', 'test-js-optimizer-si.js'), open(path_from_root('tests', 'optimizer', 'test-js-optimizer-si-output.js')).read(),
       ['simplifyIfs']),
      (path_from_root('tests', 'optimizer', 'test-js-optimizer-regs.js'), open(path_from_root('tests', 'optimizer', 'test-js-optimizer-regs-output.js')).read(),
       ['registerize']),
      (path_from_root('tests', 'optimizer', 'eliminator-test.js'), open(path_from_root('tests', 'optimizer', 'eliminator-test-output.js')).read(),
       ['eliminate']),
      (path_from_root('tests', 'optimizer', 'safe-eliminator-test.js'), open(path_from_root('tests', 'optimizer', 'safe-eliminator-test-output.js')).read(),
       ['eliminateMemSafe']),
      (path_from_root('tests', 'optimizer', 'asm-eliminator-test.js'), open(path_from_root('tests', 'optimizer', 'asm-eliminator-test-output.js')).read(),
       ['asm', 'eliminate']),
      (path_from_root('tests', 'optimizer', 'test-js-optimizer-asm-regs.js'), open(path_from_root('tests', 'optimizer', 'test-js-optimizer-asm-regs-output.js')).read(),
       ['asm', 'registerize']),
      (path_from_root('tests', 'optimizer', 'test-js-optimizer-asm-regs-harder.js'), [open(path_from_root('tests', 'optimizer', 'test-js-optimizer-asm-regs-harder-output.js')).read(), open(path_from_root('tests', 'optimizer', 'test-js-optimizer-asm-regs-harder-output2.js')).read(), open(path_from_root('tests', 'optimizer', 'test-js-optimizer-asm-regs-harder-output3.js')).read()],
       ['asm', 'registerizeHarder']),
      (path_from_root('tests', 'optimizer', 'test-js-optimizer-asm-regs-min.js'), open(path_from_root('tests', 'optimizer', 'test-js-optimizer-asm-regs-min-output.js')).read(),
       ['asm', 'registerize', 'minifyLocals']),
      (path_from_root('tests', 'optimizer', 'test-js-optimizer-asm-pre.js'), [open(path_from_root('tests', 'optimizer', 'test-js-optimizer-asm-pre-output.js')).read(), open(path_from_root('tests', 'optimizer', 'test-js-optimizer-asm-pre-output2.js')).read()],
       ['asm', 'simplifyExpressions']),
      (path_from_root('tests', 'optimizer', 'test-js-optimizer-asm-pre-f32.js'), open(path_from_root('tests', 'optimizer', 'test-js-optimizer-asm-pre-output-f32.js')).read(),
       ['asm', 'asmPreciseF32', 'simplifyExpressions', 'optimizeFrounds']),
      (path_from_root('tests', 'optimizer', 'test-js-optimizer-asm-pre-f32.js'), open(path_from_root('tests', 'optimizer', 'test-js-optimizer-asm-pre-output-f32-nosimp.js')).read(),
       ['asm', 'asmPreciseF32', 'optimizeFrounds']),
      (path_from_root('tests', 'optimizer', 'test-reduce-dead-float-return.js'), open(path_from_root('tests', 'optimizer', 'test-reduce-dead-float-return-output.js')).read(),
       ['asm', 'optimizeFrounds', 'registerizeHarder']),
      (path_from_root('tests', 'optimizer', 'test-no-reduce-dead-float-return-to-nothing.js'), open(path_from_root('tests', 'optimizer', 'test-no-reduce-dead-float-return-to-nothing-output.js')).read(),
       ['asm', 'registerizeHarder']),
      (path_from_root('tests', 'optimizer', 'test-js-optimizer-asm-last.js'), [open(path_from_root('tests', 'optimizer', 'test-js-optimizer-asm-lastOpts-output.js')).read(), open(path_from_root('tests', 'optimizer', 'test-js-optimizer-asm-lastOpts-output2.js')).read(), open(path_from_root('tests', 'optimizer', 'test-js-optimizer-asm-lastOpts-output3.js')).read()],
       ['asm', 'asmLastOpts']),
      (path_from_root('tests', 'optimizer', 'asmLastOpts.js'), open(path_from_root('tests', 'optimizer', 'asmLastOpts-output.js')).read(),
       ['asm', 'asmLastOpts']),
      (path_from_root('tests', 'optimizer', 'test-js-optimizer-asm-last.js'), [open(path_from_root('tests', 'optimizer', 'test-js-optimizer-asm-last-output.js')).read(), open(path_from_root('tests', 'optimizer', 'test-js-optimizer-asm-last-output2.js')).read(), open(path_from_root('tests', 'optimizer', 'test-js-optimizer-asm-last-output3.js')).read()],
       ['asm', 'asmLastOpts', 'last']),
      (path_from_root('tests', 'optimizer', 'test-js-optimizer-asm-relocate.js'), open(path_from_root('tests', 'optimizer', 'test-js-optimizer-asm-relocate-output.js')).read(),
       ['asm', 'relocate']),
      (path_from_root('tests', 'optimizer', 'test-js-optimizer-asm-outline1.js'), open(path_from_root('tests', 'optimizer', 'test-js-optimizer-asm-outline1-output.js')).read(),
       ['asm', 'outline']),
      (path_from_root('tests', 'optimizer', 'test-js-optimizer-asm-outline2.js'), open(path_from_root('tests', 'optimizer', 'test-js-optimizer-asm-outline2-output.js')).read(),
       ['asm', 'outline']),
      (path_from_root('tests', 'optimizer', 'test-js-optimizer-asm-outline3.js'), open(path_from_root('tests', 'optimizer', 'test-js-optimizer-asm-outline3-output.js')).read(),
       ['asm', 'outline']),
      (path_from_root('tests', 'optimizer', 'test-js-optimizer-asm-outline4.js'), open(path_from_root('tests', 'optimizer', 'test-js-optimizer-asm-outline4-output.js')).read(),
       ['asm', 'outline']),
      (path_from_root('tests', 'optimizer', 'test-js-optimizer-asm-minlast.js'), open(path_from_root('tests', 'optimizer', 'test-js-optimizer-asm-minlast-output.js')).read(),
       ['asm', 'minifyWhitespace', 'asmLastOpts', 'last']),
      (path_from_root('tests', 'optimizer', 'test-js-optimizer-shiftsAggressive.js'), open(path_from_root('tests', 'optimizer', 'test-js-optimizer-shiftsAggressive-output.js')).read(),
       ['asm', 'aggressiveVariableElimination']),
      (path_from_root('tests', 'optimizer', 'test-js-optimizer-localCSE.js'), open(path_from_root('tests', 'optimizer', 'test-js-optimizer-localCSE-output.js')).read(),
       ['asm', 'localCSE']),
      (path_from_root('tests', 'optimizer', 'test-js-optimizer-ensureLabelSet.js'), open(path_from_root('tests', 'optimizer', 'test-js-optimizer-ensureLabelSet-output.js')).read(),
       ['asm', 'ensureLabelSet']),
      (path_from_root('tests', 'optimizer', '3154.js'), open(path_from_root('tests', 'optimizer', '3154-output.js')).read(),
       ['asm', 'eliminate', 'registerize', 'asmLastOpts', 'last']),
      (path_from_root('tests', 'optimizer', 'simd.js'), open(path_from_root('tests', 'optimizer', 'simd-output.js')).read(),
       ['asm', 'eliminate']), # eliminate, just enough to trigger asm normalization/denormalization
      (path_from_root('tests', 'optimizer', 'simd.js'), open(path_from_root('tests', 'optimizer', 'simd-output-memSafe.js')).read(),
       ['asm', 'eliminateMemSafe']),
      (path_from_root('tests', 'optimizer', 'safeLabelSetting.js'), open(path_from_root('tests', 'optimizer', 'safeLabelSetting-output.js')).read(),
       ['asm', 'safeLabelSetting']), # eliminate, just enough to trigger asm normalization/denormalization
      (path_from_root('tests', 'optimizer', 'null_if.js'), [open(path_from_root('tests', 'optimizer', 'null_if-output.js')).read(), open(path_from_root('tests', 'optimizer', 'null_if-output2.js')).read()],
       ['asm', 'registerizeHarder', 'asmLastOpts', 'minifyWhitespace']), # issue 3520
      (path_from_root('tests', 'optimizer', 'null_else.js'), [open(path_from_root('tests', 'optimizer', 'null_else-output.js')).read(), open(path_from_root('tests', 'optimizer', 'null_else-output2.js')).read()],
       ['asm', 'registerizeHarder', 'asmLastOpts', 'minifyWhitespace']), # issue 3549
      (path_from_root('tests', 'optimizer', 'test-js-optimizer-splitMemory.js'), open(path_from_root('tests', 'optimizer', 'test-js-optimizer-splitMemory-output.js')).read(),
       ['splitMemory']),
      (path_from_root('tests', 'optimizer', 'JSDCE.js'), open(path_from_root('tests', 'optimizer', 'JSDCE-output.js')).read(),
       ['JSDCE']),
      (path_from_root('tests', 'optimizer', 'JSDCE-uglifyjsNodeTypes.js'), open(path_from_root('tests', 'optimizer', 'JSDCE-uglifyjsNodeTypes-output.js')).read(),
       ['JSDCE']),
      (path_from_root('tests', 'optimizer', 'JSDCE-hasOwnProperty.js'), open(path_from_root('tests', 'optimizer', 'JSDCE-hasOwnProperty-output.js')).read(),
       ['JSDCE']),
      (path_from_root('tests', 'optimizer', 'AJSDCE.js'), open(path_from_root('tests', 'optimizer', 'AJSDCE-output.js')).read(),
       ['AJSDCE']),
      (path_from_root('tests', 'optimizer', 'emitDCEGraph.js'), open(path_from_root('tests', 'optimizer', 'emitDCEGraph-output.js')).read(),
       ['emitDCEGraph', 'noEmitAst']),
      (path_from_root('tests', 'optimizer', 'emitDCEGraph2.js'), open(path_from_root('tests', 'optimizer', 'emitDCEGraph2-output.js')).read(),
       ['emitDCEGraph', 'noEmitAst']),
      (path_from_root('tests', 'optimizer', 'emitDCEGraph3.js'), open(path_from_root('tests', 'optimizer', 'emitDCEGraph3-output.js')).read(),
       ['emitDCEGraph', 'noEmitAst']),
      (path_from_root('tests', 'optimizer', 'emitDCEGraph4.js'), open(path_from_root('tests', 'optimizer', 'emitDCEGraph4-output.js')).read(),
       ['emitDCEGraph', 'noEmitAst']),
      (path_from_root('tests', 'optimizer', 'emitDCEGraph5.js'), open(path_from_root('tests', 'optimizer', 'emitDCEGraph5-output.js')).read(),
       ['emitDCEGraph', 'noEmitAst']),
      (path_from_root('tests', 'optimizer', 'applyDCEGraphRemovals.js'), open(path_from_root('tests', 'optimizer', 'applyDCEGraphRemovals-output.js')).read(),
       ['applyDCEGraphRemovals']),
      (path_from_root('tests', 'optimizer', 'detectSign-modulus-emterpretify.js'), open(path_from_root('tests', 'optimizer', 'detectSign-modulus-emterpretify-output.js')).read(),
       ['noPrintMetadata', 'emterpretify', 'noEmitAst']),
    ]:
      print(input, passes)

      if not isinstance(expected, list): expected = [expected]
      expected = [out.replace('\n\n', '\n').replace('\n\n', '\n') for out in expected]

      # test calling js optimizer
      print('  js')
      output = run_process(NODE_JS + [path_from_root('tools', 'js-optimizer.js'), input] + passes, stdin=PIPE, stdout=PIPE).stdout

      def check_js(js, expected):
        #print >> sys.stderr, 'chak\n==========================\n', js, '\n===========================\n'
        if 'registerizeHarder' in passes:
          # registerizeHarder is hard to test, as names vary by chance, nondeterminstically FIXME
          def fix(src):
            if type(src) is list:
              return list(map(fix, src))
            src = '\n'.join([line for line in src.split('\n') if 'var ' not in line]) # ignore vars
            def reorder(func):
              def swap(func, stuff):
                # emit EYE_ONE always before EYE_TWO, replacing i1,i2 or i2,i1 etc
                for i in stuff:
                  if i not in func: return func
                indexes = [[i, func.index(i)] for i in stuff]
                indexes.sort(key=lambda x: x[1])
                for j in range(len(indexes)):
                  func = func.replace(indexes[j][0], 'STD_' + str(j))
                return func
              func = swap(func, ['i1', 'i2', 'i3'])
              func = swap(func, ['i1', 'i2'])
              func = swap(func, ['i4', 'i5'])
              return func
            src = 'function '.join(map(reorder, src.split('function ')))
            return src
          js = fix(js)
          expected = fix(expected)
        self.assertIdentical(expected, js.replace('\r\n', '\n').replace('\n\n', '\n').replace('\n\n', '\n'))

      if input not in [ # blacklist of tests that are native-optimizer only
        path_from_root('tests', 'optimizer', 'asmLastOpts.js'),
        path_from_root('tests', 'optimizer', '3154.js')
      ]:
        check_js(output, expected)
      else:
        print('(skip non-native)')

      if tools.js_optimizer.use_native(passes) and tools.js_optimizer.get_native_optimizer():
        # test calling native
        def check_json():
          Popen(listify(NODE_JS) + [path_from_root('tools', 'js-optimizer.js'), output_temp, 'receiveJSON'], stdin=PIPE, stdout=open(output_temp + '.js', 'w')).communicate()
          output = open(output_temp + '.js').read()
          check_js(output, expected)

        self.clear()
        input_temp = 'temp.js'
        output_temp = 'output.js'
        shutil.copyfile(input, input_temp)
        Popen(listify(NODE_JS) + [path_from_root('tools', 'js-optimizer.js'), input_temp, 'emitJSON'], stdin=PIPE, stdout=open(input_temp + '.js', 'w')).communicate()
        original = open(input).read()
        if '// EXTRA_INFO:' in original:
          json = open(input_temp + '.js').read()
          json += '\n' + original[original.find('// EXTRA_INFO:'):]
          open(input_temp + '.js', 'w').write(json)

        # last is only relevant when we emit JS
        if 'last' not in passes and \
           'null_if' not in input and 'null_else' not in input:  # null-* tests are js optimizer or native, not a mixture (they mix badly)
          print('  native (receiveJSON)')
          output = run_process([tools.js_optimizer.get_native_optimizer(), input_temp + '.js'] + passes + ['receiveJSON', 'emitJSON'], stdin=PIPE, stdout=open(output_temp, 'w')).stdout
          check_json()

          print('  native (parsing JS)')
          output = run_process([tools.js_optimizer.get_native_optimizer(), input] + passes + ['emitJSON'], stdin=PIPE, stdout=open(output_temp, 'w')).stdout
          check_json()

        print('  native (emitting JS)')
        output = run_process([tools.js_optimizer.get_native_optimizer(), input] + passes, stdin=PIPE, stdout=PIPE).stdout
        check_js(output, expected)

  def test_m_mm(self):
    open(os.path.join(self.get_dir(), 'foo.c'), 'w').write('''#include <emscripten.h>''')
    for opt in ['M', 'MM']:
      proc = run_process([PYTHON, EMCC, os.path.join(self.get_dir(), 'foo.c'), '-' + opt], stdout=PIPE, stderr=PIPE)
      assert 'foo.o: ' in proc.stdout, '-%s failed to produce the right output: %s' % (opt, proc.stdout)
      assert 'error' not in proc.stderr, 'Unexpected stderr: ' + proc.stderr

  @unittest.skipIf('EMCC_DEBUG' in os.environ, 'cannot run in debug mode')
  def test_emcc_debug_files(self):
    for opts in [0, 1, 2, 3]:
      for debug in [None, '1', '2']:
        print(opts, debug)
        try:
          if debug: os.environ['EMCC_DEBUG'] = debug
          with clean_write_access_to_canonical_temp_dir(self.canonical_temp_dir):
            check_execute([PYTHON, EMCC, path_from_root('tests', 'hello_world.cpp'), '-O'+ str(opts)], stderr=PIPE)
            if debug is None:
              for x in os.listdir(self.canonical_temp_dir):
                if x.startswith('emcc-'):
                  assert 0
            elif debug == '1':
              assert os.path.exists(os.path.join(self.canonical_temp_dir, 'emcc-0-linktime.bc'))
              assert os.path.exists(os.path.join(self.canonical_temp_dir, 'emcc-1-original.js'))
            elif debug == '2':
              assert os.path.exists(os.path.join(self.canonical_temp_dir, 'emcc-0-basebc.bc'))
              assert os.path.exists(os.path.join(self.canonical_temp_dir, 'emcc-1-linktime.bc'))
              assert os.path.exists(os.path.join(self.canonical_temp_dir, 'emcc-2-original.js'))
        finally:
          if debug: del os.environ['EMCC_DEBUG']

  @unittest.skipIf('EMCC_DEBUG' in os.environ, 'cannot run in debug mode')
  def test_debuginfo(self):
    try:
      os.environ['EMCC_DEBUG'] = '1'
      for args, expect_llvm in [
          (['-O0'], False),
          (['-O0', '-g'], True),
          (['-O0', '-g4'], True),
          (['-O1'], False),
          (['-O1', '-g'], True),
          (['-O2'], False),
          (['-O2', '-g'], True),
        ]:
        print(args, expect_llvm)
        with clean_write_access_to_canonical_temp_dir(self.canonical_temp_dir):
          err = run_process([PYTHON, EMCC, path_from_root('tests', 'hello_world.cpp')] + args, stdout=PIPE, stderr=PIPE).stderr
        assert expect_llvm == ('strip-debug' not in err), err
    finally:
      del os.environ['EMCC_DEBUG']

  def test_scons(self): # also incidentally tests c++11 integration in llvm 3.1
    scons_path = Building.which('scons')
    if not scons_path:
      self.skipTest('Skipping other.test_scons: The tool "scons" was not found in PATH!')
    try_delete(os.path.join(self.get_dir(), 'test'))
    shutil.copytree(path_from_root('tests', 'scons'), os.path.join(self.get_dir(), 'test'))
    shutil.copytree(path_from_root('tools', 'scons', 'site_scons'), os.path.join(self.get_dir(), 'test', 'site_scons'))
    os.chdir(os.path.join(self.get_dir(), 'test'))
    Popen(['scons']).communicate()
    output = run_js('scons_integration.js', assert_returncode=5)
    assert 'If you see this - the world is all right!' in output

  def test_embind(self):
    environ = os.environ.copy()
    environ['EMCC_CLOSURE_ARGS'] = environ.get('EMCC_CLOSURE_ARGS', '') + " --externs " + pipes.quote(path_from_root('tests', 'embind', 'underscore-externs.js'))
    test_cases = [
        ([], True), # without --bind, we fail
        (['--bind'], False),
        (['--bind', '-O1'], False),
        (['--bind', '-O2'], False),
        (['--bind', '-O2', '-s', 'ALLOW_MEMORY_GROWTH=1', path_from_root('tests', 'embind', 'isMemoryGrowthEnabled=true.cpp')], False),
    ]
    without_utf8_args = ['-s', 'EMBIND_STD_STRING_IS_UTF8=0']
    test_cases_without_utf8 = []
    for args, fail in test_cases:
        test_cases_without_utf8.append((args + without_utf8_args, fail))
    test_cases += test_cases_without_utf8
    test_cases.extend([ (args[:] + ['-s', 'NO_DYNAMIC_EXECUTION=1'], status) for args, status in test_cases])
    test_cases.append((['--bind', '-O2', '--closure', '1'], False)) # closure compiler doesn't work with NO_DYNAMIC_EXECUTION=1
    test_cases = [(args + ['-s', 'IN_TEST_HARNESS=1'], status) for args, status in test_cases]

    for args, fail in test_cases:
      print(args, fail)
      self.clear()
      try_delete(self.in_dir('a.out.js'))

      testFiles = [
        path_from_root('tests', 'embind', 'underscore-1.4.2.js'),
        path_from_root('tests', 'embind', 'imvu_test_adapter.js'),
        path_from_root('tests', 'embind', 'embind.test.js'),
      ]

      Popen(
        [ PYTHON,
          EMCC,
          path_from_root('tests', 'embind', 'embind_test.cpp'),
          '--pre-js', path_from_root('tests', 'embind', 'test.pre.js'),
          '--post-js', path_from_root('tests', 'embind', 'test.post.js'),
          '-s', 'BINARYEN_ASYNC_COMPILATION=0'
        ] + args,
        stderr=PIPE if fail else None,
        env=environ).communicate()

      assert os.path.exists(self.in_dir('a.out.js')) == (not fail)
      if not fail:
        with open(self.in_dir('a.out.js'), 'ab') as f:
          for tf in testFiles:
            f.write(open(tf, 'rb').read())

        output = run_js(self.in_dir('a.out.js'), stdout=PIPE, stderr=PIPE, full_output=True, assert_returncode=0, engine=NODE_JS)
        assert "FAIL" not in output, output

  def test_llvm_nativizer(self):
    if WINDOWS: self.skipTest('test_llvm_nativizer does not work on Windows: https://github.com/kripken/emscripten/issues/702')
    if MACOS: self.skipTest('test_llvm_nativizer does not work on macOS: https://github.com/kripken/emscripten/issues/709')
    try:
      Popen(['as', '--version'], stdout=PIPE, stderr=PIPE).communicate()
    except:
      self.skipTest('no gnu as, cannot run nativizer')

    # avoid impure_ptr problems etc.
    shutil.copyfile(path_from_root('tests', 'files.cpp'), os.path.join(self.get_dir(), 'files.cpp'))
    open(os.path.join(self.get_dir(), 'somefile.binary'), 'w').write('''waka waka############################''')
    open(os.path.join(self.get_dir(), 'test.file'), 'w').write('''ay file..............,,,,,,,,,,,,,,''')
    open(os.path.join(self.get_dir(), 'stdin'), 'w').write('''inter-active''')
    subprocess.check_call([PYTHON, EMCC, os.path.join(self.get_dir(), 'files.cpp'), '-c'])
    subprocess.check_call([PYTHON, path_from_root('tools', 'nativize_llvm.py'), os.path.join(self.get_dir(), 'files.o')])
    output = run_process([os.path.join(self.get_dir(), 'files.o.run')], stdin=open(os.path.join(self.get_dir(), 'stdin')), stdout=PIPE, stderr=PIPE)
    self.assertContained('''size: 37
data: 119,97,107,97,32,119,97,107,97,35,35,35,35,35,35,35,35,35,35,35,35,35,35,35,35,35,35,35,35,35,35,35,35,35,35,35,35
loop: 119 97 107 97 32 119 97 107 97 35 35 35 35 35 35 35 35 35 35 35 35 35 35 35 35 35 35 35 35 35 35 35 35 35 35 35 35 ''' + '''
input:inter-active
texto
$
5 : 10,30,20,11,88
other=ay file...
seeked= file.
''', output.stdout)
    self.assertContained('texte\n', output.stderr)

  def test_emconfig(self):
    output = run_process([PYTHON, EMCONFIG, 'LLVM_ROOT'], stdout=PIPE, stderr=PIPE).stdout.strip()
    try:
      assert output == LLVM_ROOT
    except:
      print('Assertion failed: python %s LLVM_ROOT returned "%s" instead of expected "%s"!' % (EMCONFIG, output, LLVM_ROOT), file=sys.stderr)
      raise
    invalid = 'Usage: em-config VAR_NAME'
    # Don't accept variables that do not exist
    output = run_process([PYTHON, EMCONFIG, 'VAR_WHICH_DOES_NOT_EXIST'], stdout=PIPE, stderr=PIPE, check=False).stdout.strip()
    assert output == invalid
    # Don't accept no arguments
    output = run_process([PYTHON, EMCONFIG], stdout=PIPE, stderr=PIPE, check=False).stdout.strip()
    assert output == invalid
    # Don't accept more than one variable
    output = run_process([PYTHON, EMCONFIG, 'LLVM_ROOT', 'EMCC'], stdout=PIPE, stderr=PIPE, check=False).stdout.strip()
    assert output == invalid
    # Don't accept arbitrary python code
    output = run_process([PYTHON, EMCONFIG, 'sys.argv[1]'], stdout=PIPE, stderr=PIPE, check=False).stdout.strip()
    assert output == invalid

  def test_link_s(self):
    # -s OPT=VALUE can conflict with -s as a linker option. We warn and ignore
    open(os.path.join(self.get_dir(), 'main.cpp'), 'w').write(r'''
      extern "C" {
        void something();
      }

      int main() {
        something();
        return 0;
      }
    ''')
    open(os.path.join(self.get_dir(), 'supp.cpp'), 'w').write(r'''
      #include <stdio.h>

      extern "C" {
        void something() {
          printf("yello\n");
        }
      }
    ''')
    Popen([PYTHON, EMCC, os.path.join(self.get_dir(), 'main.cpp'), '-o', 'main.o']).communicate()
    Popen([PYTHON, EMCC, os.path.join(self.get_dir(), 'supp.cpp'), '-o', 'supp.o']).communicate()

    output = Popen([PYTHON, EMCC, os.path.join(self.get_dir(), 'main.o'), '-s', os.path.join(self.get_dir(), 'supp.o'), '-s', 'SAFE_HEAP=1'], stderr=PIPE).communicate()
    output = run_js('a.out.js')
    assert 'yello' in output, 'code works'
    code = open('a.out.js').read()
    assert 'SAFE_HEAP' in code, 'valid -s option had an effect'

  def test_conftest_s_flag_passing(self):
    open(os.path.join(self.get_dir(), 'conftest.c'), 'w').write(r'''
      int main() {
        return 0;
      }
    ''')
    os.environ["EMMAKEN_JUST_CONFIGURE"] = "1"
    cmd = [PYTHON, EMCC, '-s', 'ASSERTIONS=1', os.path.join(self.get_dir(), 'conftest.c'), '-o', 'conftest']
    output = run_process(cmd, stderr=PIPE)
    del os.environ["EMMAKEN_JUST_CONFIGURE"]
    self.assertNotContained('emcc: warning: treating -s as linker option', output.stderr)
    assert os.path.exists('conftest')

  def test_file_packager(self):
    try:
      os.mkdir('subdir')
    except:
      pass
    open('data1.txt', 'w').write('data1')
    os.chdir('subdir')
    open('data2.txt', 'w').write('data2')
    # relative path to below the current dir is invalid
    output = run_process([PYTHON, FILE_PACKAGER, 'test.data', '--preload', '../data1.txt'], stdout=PIPE, stderr=PIPE, check=False)
    assert len(output.stdout) == 0
    assert 'below the current directory' in output.stderr
    # relative path that ends up under us is cool
    output = run_process([PYTHON, FILE_PACKAGER, 'test.data', '--preload', '../subdir/data2.txt'], stdout=PIPE, stderr=PIPE)
    assert len(output.stdout)
    assert 'below the current directory' not in output.stderr
    # direct path leads to the same code being generated - relative path does not make us do anything different
    output2 = run_process([PYTHON, FILE_PACKAGER, 'test.data', '--preload', 'data2.txt'], stdout=PIPE, stderr=PIPE)
    assert len(output2.stdout)
    assert 'below the current directory' not in output2.stderr
    def clean(txt):
      return [line for line in txt.split('\n') if 'PACKAGE_UUID' not in line and 'loadPackage({' not in line]
    assert clean(output.stdout) == clean(output2.stdout)
    # verify '--separate-metadata' option produces separate metadata file
    os.chdir('..')
    Popen([PYTHON, FILE_PACKAGER, 'test.data', '--preload', 'data1.txt', '--preload', 'subdir/data2.txt', '--js-output=immutable.js', '--separate-metadata']).communicate()
    assert os.path.isfile('immutable.js.metadata')
    # verify js output file is immutable when metadata is separated
    shutil.copy2('immutable.js', 'immutable.js.copy') # copy with timestamp preserved
    Popen([PYTHON, FILE_PACKAGER, 'test.data', '--preload', 'data1.txt', '--preload', 'subdir/data2.txt', '--js-output=immutable.js', '--separate-metadata']).communicate()
    import filecmp
    assert filecmp.cmp('immutable.js.copy', 'immutable.js')
    assert str(os.path.getmtime('immutable.js.copy')) == str(os.path.getmtime('immutable.js')) # assert both file content and timestamp are the same as reference copy
    # verify the content of metadata file is correct
    f = open('immutable.js.metadata', 'r')
    import json
    metadata = json.load(f)
    f.close
    assert len(metadata['files']) == 2
    assert metadata['files'][0]['start'] == 0 and metadata['files'][0]['end'] == len('data1') and metadata['files'][0]['filename'] == '/data1.txt'
    assert metadata['files'][1]['start'] == len('data1') and metadata['files'][1]['end'] == len('data1') + len('data2') and metadata['files'][1]['filename'] == '/subdir/data2.txt'
    assert metadata['remote_package_size'] == len('data1') + len('data2')
    import uuid
    try:
      uuid = uuid.UUID(metadata['package_uuid'], version = 4) # can only assert the uuid format is correct, the uuid's value is expected to differ in between invocation
    except ValueError:
      assert False

  def test_file_packager_unicode(self):
    unicode_name = 'unicode…☃'
    if not os.path.exists(unicode_name):
      try:
        os.mkdir(unicode_name)
      except:
        print("we failed to even create a unicode dir, so on this OS, we can't test this")
        return
    full = os.path.join(unicode_name, 'data.txt')
    open(full, 'w').write('data')
    proc = run_process([PYTHON, FILE_PACKAGER, 'test.data', '--preload', full], stdout=PIPE, stderr=PIPE)
    assert len(proc.stdout), proc.stderr
    assert unicode_name in proc.stdout, proc.stdout
    print(len(proc.stderr))

  def test_file_packager_mention_FORCE_FILESYSTEM(self):
    MESSAGE = 'Remember to build the main file with  -s FORCE_FILESYSTEM=1  so that it includes support for loading this file package'
    open('data.txt', 'w').write('data1')
    # mention when running standalone
    err = run_process([PYTHON, FILE_PACKAGER, 'test.data', '--preload', 'data.txt'], stdout=PIPE, stderr=PIPE).stderr
    self.assertContained(MESSAGE, err)
    # do not mention from emcc
    err = run_process([PYTHON, EMCC, path_from_root('tests', 'hello_world.c'), '--preload-file', 'data.txt'], stdout=PIPE, stderr=PIPE).stderr
    assert len(err) == 0, err

  def test_headless(self):
    shutil.copyfile(path_from_root('tests', 'screenshot.png'), os.path.join(self.get_dir(), 'example.png'))
    Popen([PYTHON, EMCC, path_from_root('tests', 'sdl_headless.c'), '-s', 'HEADLESS=1']).communicate()
    output = run_js('a.out.js', stderr=PIPE)
    assert '''Init: 0
Font: 0x1
Sum: 0
you should see two lines of text in different colors and a blue rectangle
SDL_Quit called (and ignored)
done.
''' in output, output

  def test_preprocess(self):
    self.clear()

    out = run_process([PYTHON, EMCC, path_from_root('tests', 'hello_world.c'), '-E'], stdout=PIPE).stdout
    assert not os.path.exists('a.out.js')
    # Test explicitly that the output contains a line typically written by the preprocessor.
    # Clang outputs on Windows lines like "#line 1", on Unix '# 1 '.
    # TODO: This is one more of those platform-specific discrepancies, investigate more if this ever becomes an issue,
    # ideally we would have emcc output identical data on all platforms.
    assert '''#line 1 ''' in out or '''# 1 ''' in out
    assert '''hello_world.c"''' in out
    assert '''printf("hello, world!''' in out

  def test_demangle(self):
    open('src.cpp', 'w').write('''
      #include <stdio.h>
      #include <emscripten.h>
      void two(char c) {
        EM_ASM(out(stackTrace()));
      }
      void one(int x) {
        two(x % 17);
      }
      int main() {
        EM_ASM(out(demangle('__Znwm'))); // check for no aborts
        EM_ASM(out(demangle('_main')));
        EM_ASM(out(demangle('__Z2f2v')));
        EM_ASM(out(demangle('__Z12abcdabcdabcdi')));
        EM_ASM(out(demangle('__ZL12abcdabcdabcdi')));
        EM_ASM(out(demangle('__Z4testcsifdPvPiPc')));
        EM_ASM(out(demangle('__ZN4test5moarrEcslfdPvPiPc')));
        EM_ASM(out(demangle('__ZN4Waka1f12a234123412345pointEv')));
        EM_ASM(out(demangle('__Z3FooIiEvv')));
        EM_ASM(out(demangle('__Z3FooIidEvi')));
        EM_ASM(out(demangle('__ZN3Foo3BarILi5EEEvv')));
        EM_ASM(out(demangle('__ZNK10__cxxabiv120__si_class_type_info16search_below_dstEPNS_19__dynamic_cast_infoEPKvib')));
        EM_ASM(out(demangle('__Z9parsewordRPKciRi')));
        EM_ASM(out(demangle('__Z5multiwahtjmxyz')));
        EM_ASM(out(demangle('__Z1aA32_iPA5_c')));
        EM_ASM(out(demangle('__ZN21FWakaGLXFleeflsMarfooC2EjjjPKvbjj')));
        EM_ASM(out(demangle('__ZN5wakaw2Cm10RasterBaseINS_6watwat9PolocatorEE8merbine1INS4_2OREEEvPKjj'))); // we get this wrong, but at least emit a '?'
        one(17);
        return 0;
      }
    ''')

    # full demangle support

    run_process([PYTHON, EMCC, 'src.cpp', '-s', 'DEMANGLE_SUPPORT=1'])
    output = run_js('a.out.js')
    self.assertContained('''operator new(unsigned long)
_main
f2()
abcdabcdabcd(int)
abcdabcdabcd(int)
test(char, short, int, float, double, void*, int*, char*)
test::moarr(char, short, long, float, double, void*, int*, char*)
Waka::f::a23412341234::point()
void Foo<int>()
void Foo<int, double>(int)
void Foo::Bar<5>()
__cxxabiv1::__si_class_type_info::search_below_dst(__cxxabiv1::__dynamic_cast_info*, void const*, int, bool) const
parseword(char const*&, int, int&)
multi(wchar_t, signed char, unsigned char, unsigned short, unsigned int, unsigned long, long long, unsigned long long, ...)
a(int [32], char (*) [5])
FWakaGLXFleeflsMarfoo::FWakaGLXFleeflsMarfoo(unsigned int, unsigned int, unsigned int, void const*, bool, unsigned int, unsigned int)
void wakaw::Cm::RasterBase<wakaw::watwat::Polocator>::merbine1<wakaw::Cm::RasterBase<wakaw::watwat::Polocator>::OR>(unsigned int const*, unsigned int)
''', output)
    # test for multiple functions in one stack trace
    run_process([PYTHON, EMCC, 'src.cpp', '-s', 'DEMANGLE_SUPPORT=1', '-g'])
    output = run_js('a.out.js')
    assert 'one(int)' in output, out
    assert 'two(char)' in output, out

  def test_demangle_cpp(self):
    open('src.cpp', 'w').write('''
      #include <stdio.h>
      #include <emscripten.h>
      #include <cxxabi.h>
      #include <assert.h>

      int main() {
        char out[256];
        int status = 1;
        size_t length = 255;
        abi::__cxa_demangle("_ZN4Waka1f12a234123412345pointEv", out, &length, &status);
        assert(status == 0);
        printf("%s\\n", out);
        return 0;
      }
    ''')

    Popen([PYTHON, EMCC, 'src.cpp']).communicate()
    output = run_js('a.out.js')
    self.assertContained('Waka::f::a23412341234::point()', output)

  def test_module_exports_with_closure(self):
    # This test checks that module.export is retained when JavaScript is minified by compiling with --closure 1
    # This is important as if module.export is not present the Module object will not be visible to node.js
    # Run with ./runner.py other.test_module_exports_with_closure

    # First make sure test.js isn't present.
    self.clear()

    # compile with -O2 --closure 0
    Popen([PYTHON, EMCC, path_from_root('tests', 'Module-exports', 'test.c'), '-o', 'test.js', '-O2', '--closure', '0', '--pre-js', path_from_root('tests', 'Module-exports', 'setup.js'), '-s', 'EXPORTED_FUNCTIONS=["_bufferTest"]', '-s', 'EXTRA_EXPORTED_RUNTIME_METHODS=["ccall", "cwrap"]', '-s', 'BINARYEN_ASYNC_COMPILATION=0'], stdout=PIPE, stderr=PIPE).communicate()

    # Check that compilation was successful
    assert os.path.exists('test.js')
    test_js_closure_0 = open('test.js').read()

    # Check that test.js compiled with --closure 0 contains "module['exports'] = Module;"
    assert ("module['exports'] = Module;" in test_js_closure_0) or ('module["exports"]=Module' in test_js_closure_0) or ('module["exports"] = Module;' in test_js_closure_0)

    # Check that main.js (which requires test.js) completes successfully when run in node.js
    # in order to check that the exports are indeed functioning correctly.
    shutil.copyfile(path_from_root('tests', 'Module-exports', 'main.js'), 'main.js')
    if NODE_JS in JS_ENGINES:
      self.assertContained('bufferTest finished', run_js('main.js', engine=NODE_JS))

    # Delete test.js again and check it's gone.
    try_delete(path_from_root('tests', 'Module-exports', 'test.js'))
    assert not os.path.exists(path_from_root('tests', 'Module-exports', 'test.js'))

    # compile with -O2 --closure 1
    Popen([PYTHON, EMCC, path_from_root('tests', 'Module-exports', 'test.c'), '-o', path_from_root('tests', 'Module-exports', 'test.js'), '-O2', '--closure', '1', '--pre-js', path_from_root('tests', 'Module-exports', 'setup.js'), '-s', 'EXPORTED_FUNCTIONS=["_bufferTest"]', '-s', 'BINARYEN_ASYNC_COMPILATION=0'], stdout=PIPE, stderr=PIPE).communicate()

    # Check that compilation was successful
    assert os.path.exists(path_from_root('tests', 'Module-exports', 'test.js'))
    test_js_closure_1 = open(path_from_root('tests', 'Module-exports', 'test.js')).read()

    # Check that test.js compiled with --closure 1 contains "module.exports", we want to verify that
    # "module['exports']" got minified to "module.exports" when compiling with --closure 1
    assert "module.exports" in test_js_closure_1

    # Check that main.js (which requires test.js) completes successfully when run in node.js
    # in order to check that the exports are indeed functioning correctly.
    if NODE_JS in JS_ENGINES:
      self.assertContained('bufferTest finished', run_js('main.js', engine=NODE_JS))

    # Tidy up files that might have been created by this test.
    try_delete(path_from_root('tests', 'Module-exports', 'test.js'))
    try_delete(path_from_root('tests', 'Module-exports', 'test.js.map'))
    try_delete(path_from_root('tests', 'Module-exports', 'test.js.mem'))

  def test_node_catch_exit(self):
    # Test that in node.js exceptions are not caught if NODEJS_EXIT_CATCH=0
    if NODE_JS not in JS_ENGINES:
      return

    open(os.path.join(self.get_dir(), 'count.c'), 'w').write('''
      #include <string.h>
      int count(const char *str) {
          return (int)strlen(str);
      }
    ''')

    open(os.path.join(self.get_dir(), 'index.js'), 'w').write('''
      const count = require('./count.js');

      console.log(xxx); //< here is the ReferenceError
    ''')

    reference_error_text = 'console.log(xxx); //< here is the ReferenceError';

    subprocess.check_call([PYTHON, EMCC, os.path.join(self.get_dir(), 'count.c'), '-o', 'count.js'])

    # Check that the ReferenceError is caught and rethrown and thus the original error line is masked
    self.assertNotContained(reference_error_text,
                            run_js ('index.js', engine=NODE_JS, stderr=STDOUT, assert_returncode=None))

    subprocess.check_call([PYTHON, EMCC, os.path.join(self.get_dir(), 'count.c'), '-o', 'count.js', '-s', 'NODEJS_CATCH_EXIT=0'])

    # Check that the ReferenceError is not caught, so we see the error properly
    self.assertContained(reference_error_text,
                         run_js ('index.js', engine=NODE_JS, stderr=STDOUT, assert_returncode=None))

  def test_extra_exported_methods(self):
    # Test with node.js that the EXTRA_EXPORTED_RUNTIME_METHODS setting is considered by libraries
    if NODE_JS not in JS_ENGINES:
      self.skipTest("node engine required for this test")

    open(os.path.join(self.get_dir(), 'count.c'), 'w').write('''
      #include <string.h>
      int count(const char *str) {
          return (int)strlen(str);
      }
    ''')

    open(os.path.join(self.get_dir(), 'index.js'), 'w').write('''
      const count = require('./count.js');

      console.log(count.FS_writeFile);
    ''')

    reference_error_text = 'undefined';

    subprocess.check_call([PYTHON, EMCC, os.path.join(self.get_dir(), 'count.c'), '-s', 'FORCE_FILESYSTEM=1', '-s', 'EXTRA_EXPORTED_RUNTIME_METHODS=["FS_writeFile"]','-o', 'count.js'])

    # Check that the Module.FS_writeFile exists
    self.assertNotContained(reference_error_text,
                            run_js ('index.js', engine=NODE_JS, stderr=STDOUT, assert_returncode=None))

    subprocess.check_call([PYTHON, EMCC, os.path.join(self.get_dir(), 'count.c'), '-s', 'FORCE_FILESYSTEM=1', '-o', 'count.js'])

    # Check that the Module.FS_writeFile is not exported
    self.assertContained(reference_error_text,
                         run_js ('index.js', engine=NODE_JS, stderr=STDOUT, assert_returncode=None))

  def test_fs_stream_proto(self):
    open('src.cpp', 'wb').write(br'''
#include <stdio.h>
#include <fcntl.h>
#include <unistd.h>
#include <sys/stat.h>
#include <errno.h>
#include <string.h>

int main()
{
    long file_size = 0;
    int h = open("src.cpp", O_RDONLY, 0666);
    if (0 != h)
    {
        FILE* file = fdopen(h, "rb");
        if (0 != file)
        {
            fseek(file, 0, SEEK_END);
            file_size = ftell(file);
            fseek(file, 0, SEEK_SET);
        }
        else
        {
            printf("fdopen() failed: %s\n", strerror(errno));
            return 10;
        }
        close(h);
        printf("File size: %ld\n", file_size);
    }
    else
    {
        printf("open() failed: %s\n", strerror(errno));
        return 10;
    }
    return 0;
}
    ''')
    Popen([PYTHON, EMCC, 'src.cpp', '--embed-file', 'src.cpp']).communicate()
    for engine in JS_ENGINES:
      out = run_js('a.out.js', engine=engine, stderr=PIPE, full_output=True)
      self.assertContained('File size: 724', out)

  def test_proxyfs(self):
    # This test supposes that 3 different programs share the same directory and files.
    # The same JS object is not used for each of them
    # But 'require' function caches JS objects.
    # If we just load same js-file multiple times like following code,
    # these programs (m0,m1,m2) share the same JS object.
    #
    #   var m0 = require('./proxyfs_test.js');
    #   var m1 = require('./proxyfs_test.js');
    #   var m2 = require('./proxyfs_test.js');
    #
    # To separate js-objects for each of them, following 'require' use different js-files.
    #
    #   var m0 = require('./proxyfs_test.js');
    #   var m1 = require('./proxyfs_test1.js');
    #   var m2 = require('./proxyfs_test2.js');
    #
    open('proxyfs_test_main.js', 'w').write(r'''
var m0 = require('./proxyfs_test.js');
var m1 = require('./proxyfs_test1.js');
var m2 = require('./proxyfs_test2.js');

var section;
function print(str){
  process.stdout.write(section+":"+str+":");
}

m0.FS.mkdir('/working');
m0.FS.mount(m0.PROXYFS,{root:'/',fs:m1.FS},'/working');
m0.FS.mkdir('/working2');
m0.FS.mount(m0.PROXYFS,{root:'/',fs:m2.FS},'/working2');

section = "child m1 reads and writes local file.";
print("m1 read embed");
m1.ccall('myreade','number',[],[]);
print("m1 write");console.log("");
m1.ccall('mywrite0','number',['number'],[1]);
print("m1 read");
m1.ccall('myread0','number',[],[]);


section = "child m2 reads and writes local file.";
print("m2 read embed");
m2.ccall('myreade','number',[],[]);
print("m2 write");console.log("");
m2.ccall('mywrite0','number',['number'],[2]);
print("m2 read");
m2.ccall('myread0','number',[],[]);

section = "child m1 reads local file.";
print("m1 read");
m1.ccall('myread0','number',[],[]);

section = "parent m0 reads and writes local and children's file.";
print("m0 read embed");
m0.ccall('myreade','number',[],[]);
print("m0 read m1");
m0.ccall('myread1','number',[],[]);
print("m0 read m2");
m0.ccall('myread2','number',[],[]);

section = "m0,m1 and m2 verify local files.";
print("m0 write");console.log("");
m0.ccall('mywrite0','number',['number'],[0]);
print("m0 read");
m0.ccall('myread0','number',[],[]);
print("m1 read");
m1.ccall('myread0','number',[],[]);
print("m2 read");
m2.ccall('myread0','number',[],[]);

print("m0 read embed");
m0.ccall('myreade','number',[],[]);
print("m1 read embed");
m1.ccall('myreade','number',[],[]);
print("m2 read embed");
m2.ccall('myreade','number',[],[]);

section = "parent m0 writes and reads children's files.";
print("m0 write m1");console.log("");
m0.ccall('mywrite1','number',[],[]);
print("m0 read m1");
m0.ccall('myread1','number',[],[]);
print("m0 write m2");console.log("");
m0.ccall('mywrite2','number',[],[]);
print("m0 read m2");
m0.ccall('myread2','number',[],[]);
print("m1 read");
m1.ccall('myread0','number',[],[]);
print("m2 read");
m2.ccall('myread0','number',[],[]);
print("m0 read m0");
m0.ccall('myread0','number',[],[]);
''')

    open('proxyfs_pre.js', 'w').write(r'''
if (typeof Module === 'undefined') Module = {};
Module["noInitialRun"]=true;
Module["noExitRuntime"]=true;
''')

    open('proxyfs_embed.txt', 'w').write(r'''test
''')

    open('proxyfs_test.c', 'w').write(r'''
#include <stdio.h>

int
mywrite1(){
  FILE* out = fopen("/working/hoge.txt","w");
  fprintf(out,"test1\n");
  fclose(out);
  return 0;
}

int
myread1(){
  FILE* in = fopen("/working/hoge.txt","r");
  char buf[1024];
  int len;
  if(in==NULL)
    printf("open failed\n");

  while(! feof(in)){
    if(fgets(buf,sizeof(buf),in)==buf){
      printf("%s",buf);
    }
  }
  fclose(in);
  return 0;
}
int
mywrite2(){
  FILE* out = fopen("/working2/hoge.txt","w");
  fprintf(out,"test2\n");
  fclose(out);
  return 0;
}

int
myread2(){
  {
    FILE* in = fopen("/working2/hoge.txt","r");
    char buf[1024];
    int len;
    if(in==NULL)
      printf("open failed\n");

    while(! feof(in)){
      if(fgets(buf,sizeof(buf),in)==buf){
        printf("%s",buf);
      }
    }
    fclose(in);
  }
  return 0;
}

int
mywrite0(int i){
  FILE* out = fopen("hoge.txt","w");
  fprintf(out,"test0_%d\n",i);
  fclose(out);
  return 0;
}

int
myread0(){
  {
    FILE* in = fopen("hoge.txt","r");
    char buf[1024];
    int len;
    if(in==NULL)
      printf("open failed\n");

    while(! feof(in)){
      if(fgets(buf,sizeof(buf),in)==buf){
        printf("%s",buf);
      }
    }
    fclose(in);
  }
  return 0;
}

int
myreade(){
  {
    FILE* in = fopen("proxyfs_embed.txt","r");
    char buf[1024];
    int len;
    if(in==NULL)
      printf("open failed\n");

    while(! feof(in)){
      if(fgets(buf,sizeof(buf),in)==buf){
        printf("%s",buf);
      }
    }
    fclose(in);
  }
  return 0;
}
''')

    Popen([PYTHON, EMCC,
           '-o', 'proxyfs_test.js', 'proxyfs_test.c',
           '--embed-file', 'proxyfs_embed.txt', '--pre-js', 'proxyfs_pre.js',
           '-s', 'EXTRA_EXPORTED_RUNTIME_METHODS=["ccall", "cwrap"]',
           '-s', 'BINARYEN_ASYNC_COMPILATION=0',
           '-s', 'MAIN_MODULE=1']).communicate()
    # Following shutil.copyfile just prevent 'require' of node.js from caching js-object.
    # See https://nodejs.org/api/modules.html
    shutil.copyfile('proxyfs_test.js', 'proxyfs_test1.js')
    shutil.copyfile('proxyfs_test.js', 'proxyfs_test2.js')
    out = run_js('proxyfs_test_main.js')
    section="child m1 reads and writes local file."
    self.assertContained(section+":m1 read embed:test", out)
    self.assertContained(section+":m1 write:", out)
    self.assertContained(section+":m1 read:test0_1", out)
    section="child m2 reads and writes local file."
    self.assertContained(section+":m2 read embed:test", out)
    self.assertContained(section+":m2 write:", out)
    self.assertContained(section+":m2 read:test0_2", out)
    section="child m1 reads local file."
    self.assertContained(section+":m1 read:test0_1", out)
    section="parent m0 reads and writes local and children's file."
    self.assertContained(section+":m0 read embed:test", out)
    self.assertContained(section+":m0 read m1:test0_1", out)
    self.assertContained(section+":m0 read m2:test0_2", out)
    section="m0,m1 and m2 verify local files."
    self.assertContained(section+":m0 write:", out)
    self.assertContained(section+":m0 read:test0_0", out)
    self.assertContained(section+":m1 read:test0_1", out)
    self.assertContained(section+":m2 read:test0_2", out)
    self.assertContained(section+":m0 read embed:test", out)
    self.assertContained(section+":m1 read embed:test", out)
    self.assertContained(section+":m2 read embed:test", out)
    section="parent m0 writes and reads children's files."
    self.assertContained(section+":m0 write m1:", out)
    self.assertContained(section+":m0 read m1:test1", out)
    self.assertContained(section+":m0 write m2:", out)
    self.assertContained(section+":m0 read m2:test2", out)
    self.assertContained(section+":m1 read:test1", out)
    self.assertContained(section+":m2 read:test2", out)
    self.assertContained(section+":m0 read m0:test0_0", out)

  def check_simd(self, expected_simds, expected_out):
    if SPIDERMONKEY_ENGINE in JS_ENGINES:
      out = run_js('a.out.js', engine=SPIDERMONKEY_ENGINE, stderr=PIPE, full_output=True)
      self.validate_asmjs(out)
    else:
      out = run_js('a.out.js')
    self.assertContained(expected_out, out)

    src = open('a.out.js').read()
    asm = src[src.find('// EMSCRIPTEN_START_FUNCS'):src.find('// EMSCRIPTEN_END_FUNCS')]
    simds = asm.count('SIMD_')
    assert simds >= expected_simds, 'expecting to see at least %d SIMD* uses, but seeing %d' % (expected_simds, simds)

  @unittest.skip("autovectorization of this stopped in LLVM 6.0")
  def test_autovectorize_linpack(self):
    # TODO: investigate when SIMD arrives in wasm
    Popen([PYTHON, EMCC, path_from_root('tests', 'linpack.c'), '-O2', '-s', 'SIMD=1', '-DSP', '-s', 'PRECISE_F32=1', '--profiling', '-s', 'WASM=0']).communicate()
    self.check_simd(30, 'Unrolled Single  Precision')

  def test_dependency_file(self):
    # Issue 1732: -MMD (and friends) create dependency files that need to be
    # copied from the temporary directory.

    open(os.path.join(self.get_dir(), 'test.cpp'), 'w').write(r'''
      #include "test.hpp"

      void my_function()
      {
      }
    ''')
    open(os.path.join(self.get_dir(), 'test.hpp'), 'w').write(r'''
      void my_function();
    ''')

    Popen([PYTHON, EMCC, '-MMD', '-c', os.path.join(self.get_dir(), 'test.cpp'), '-o',
      os.path.join(self.get_dir(), 'test.o')]).communicate()

    assert os.path.exists(os.path.join(self.get_dir(), 'test.d')), 'No dependency file generated'
    deps = open(os.path.join(self.get_dir(), 'test.d')).read()
    # Look for ': ' instead of just ':' to not confuse C:\path\ notation with make "target: deps" rule. Not perfect, but good enough for this test.
    head, tail = deps.split(': ', 2)
    assert 'test.o' in head, 'Invalid dependency target'
    assert 'test.cpp' in tail and 'test.hpp' in tail, 'Invalid dependencies generated'

  def test_dependency_file_2(self):
    self.clear()
    shutil.copyfile(path_from_root('tests', 'hello_world.c'), 'a.c')
    Popen([PYTHON, EMCC, 'a.c', '-MMD', '-MF', 'test.d', '-c']).communicate()
    self.assertContained(open('test.d').read(), 'a.o: a.c\n')

    self.clear()
    shutil.copyfile(path_from_root('tests', 'hello_world.c'), 'a.c')
    Popen([PYTHON, EMCC, 'a.c', '-MMD', '-MF', 'test.d', '-c', '-o', 'test.o']).communicate()
    self.assertContained(open('test.d').read(), 'test.o: a.c\n')

    self.clear()
    shutil.copyfile(path_from_root('tests', 'hello_world.c'), 'a.c')
    os.mkdir('obj')
    Popen([PYTHON, EMCC, 'a.c', '-MMD', '-MF', 'test.d', '-c', '-o', 'obj/test.o']).communicate()
    self.assertContained(open('test.d').read(), 'obj/test.o: a.c\n')

  def test_quoted_js_lib_key(self):
    open('lib.js', 'w').write(r'''
mergeInto(LibraryManager.library, {
   __internal_data:{
    '<' : 0,
    'white space' : 1
  },
  printf__deps: ['__internal_data', 'fprintf']
});
''')

    Popen([PYTHON, EMCC, path_from_root('tests', 'hello_world.cpp'), '--js-library', 'lib.js']).communicate()
    self.assertContained('hello, world!', run_js(os.path.join(self.get_dir(), 'a.out.js')))

  def test_exported_js_lib(self):
    open('lib.js', 'w').write(r'''
mergeInto(LibraryManager.library, {
 jslibfunc: function(x) { return 2*x }
});
''')
    open('src.cpp', 'w').write(r'''
#include <emscripten.h>
extern "C" int jslibfunc(int x);
int main() {
  printf("c calling: %d\n", jslibfunc(6));
  EM_ASM({
    out('js calling: ' + Module['_jslibfunc'](5) + '.');
  });
}
''')
    subprocess.check_call([PYTHON, EMCC, 'src.cpp', '--js-library', 'lib.js', '-s', 'EXPORTED_FUNCTIONS=["_main", "_jslibfunc"]'])
    self.assertContained('c calling: 12\njs calling: 10.', run_js('a.out.js'))

  def test_js_lib_using_asm_lib(self):
    open('lib.js', 'w').write(r'''
mergeInto(LibraryManager.library, {
  jslibfunc__deps: ['asmlibfunc'],
  jslibfunc: function(x) {
    return 2 * _asmlibfunc(x);
  },

  asmlibfunc__asm: true,
  asmlibfunc__sig: 'ii',
  asmlibfunc: function(x) {
    x = x | 0;
    return x + 1 | 0;
  }
});
''')
    open('src.cpp', 'w').write(r'''
#include <stdio.h>
extern "C" int jslibfunc(int x);
int main() {
  printf("c calling: %d\n", jslibfunc(6));
}
''')
    subprocess.check_call([PYTHON, EMCC, 'src.cpp', '--js-library', 'lib.js'])
    self.assertContained('c calling: 14\n', run_js('a.out.js'))

  def test_EMCC_BUILD_DIR(self):
    # EMCC_BUILD_DIR env var contains the dir we were building in, when running the js compiler (e.g. when
    # running a js library). We force the cwd to be src/ for technical reasons, so this lets you find out
    # where you were.
    open('lib.js', 'w').write(r'''
printErr('dir was ' + process.env.EMCC_BUILD_DIR);
''')
    err = run_process([PYTHON, EMCC, path_from_root('tests', 'hello_world.cpp'), '--js-library', 'lib.js'], stderr=PIPE).stderr
    self.assertContained('dir was ' + os.path.realpath(os.path.normpath(self.get_dir())), err)

  def test_float_h(self):
    process = run_process([PYTHON, EMCC, path_from_root('tests', 'float+.c')], stdout=PIPE, stderr=PIPE)
    assert process.returncode is 0, 'float.h should agree with our system: ' + process.stdout + '\n\n\n' + process.stderr

  def test_default_obj_ext(self):
    outdir = os.path.join(self.get_dir(), 'out_dir') + '/'

    self.clear()
    os.mkdir(outdir)
    err = run_process([PYTHON, EMCC, '-c', path_from_root('tests', 'hello_world.c'), '-o', outdir], stderr=PIPE).stderr
    assert not err, err
    assert os.path.isfile(outdir + 'hello_world.o')

    self.clear()
    os.mkdir(outdir)
    err = run_process([PYTHON, EMCC, '-c', path_from_root('tests', 'hello_world.c'), '-o', outdir, '--default-obj-ext', 'obj'], stderr=PIPE).stderr
    assert not err, err
    assert os.path.isfile(outdir + 'hello_world.obj')

  def test_doublestart_bug(self):
    open('code.cpp', 'w').write(r'''
#include <stdio.h>
#include <emscripten.h>

void main_loop(void) {
    static int cnt = 0;
    if (++cnt >= 10) emscripten_cancel_main_loop();
}

int main(void) {
    printf("This should only appear once.\n");
    emscripten_set_main_loop(main_loop, 10, 0);
    return 0;
}
''')

    open('pre.js', 'w').write(r'''
if (!Module['preRun']) Module['preRun'] = [];
Module["preRun"].push(function () {
    addRunDependency('test_run_dependency');
    removeRunDependency('test_run_dependency');
});
''')

    Popen([PYTHON, EMCC, 'code.cpp', '--pre-js', 'pre.js']).communicate()
    output = run_js(os.path.join(self.get_dir(), 'a.out.js'), engine=NODE_JS)

    assert output.count('This should only appear once.') == 1, '\n'+output

  def test_module_print(self):
    open('code.cpp', 'w').write(r'''
#include <stdio.h>
int main(void) {
  printf("123456789\n");
  return 0;
}
''')

    open('pre.js', 'w').write(r'''
var Module = { print: function(x) { throw '<{(' + x + ')}>' } };
''')

    Popen([PYTHON, EMCC, 'code.cpp', '--pre-js', 'pre.js']).communicate()
    output = run_js(os.path.join(self.get_dir(), 'a.out.js'), stderr=PIPE, full_output=True, engine=NODE_JS, assert_returncode=None)
    assert r'<{(123456789)}>' in output, output

  def test_precompiled_headers(self):
    for suffix in ['gch', 'pch']:
      print(suffix)
      self.clear()

      open('header.h', 'w').write('#define X 5\n')
      Popen([PYTHON, EMCC, '-xc++-header', 'header.h', '-c']).communicate()
      assert os.path.exists('header.h.gch') # default output is gch
      if suffix != 'gch':
        Popen([PYTHON, EMCC, '-xc++-header', 'header.h', '-o', 'header.h.' + suffix]).communicate()
        assert open('header.h.gch', 'rb').read() == open('header.h.' + suffix, 'rb').read()

      open('src.cpp', 'w').write(r'''
#include <stdio.h>
int main() {
  printf("|%d|\n", X);
  return 0;
}
''')
      Popen([PYTHON, EMCC, 'src.cpp', '-include', 'header.h']).communicate()

      output = run_js(self.in_dir('a.out.js'), stderr=PIPE, full_output=True, engine=NODE_JS)
      assert '|5|' in output, output

      # also verify that the gch is actually used
      err = run_process([PYTHON, EMCC, 'src.cpp', '-include', 'header.h', '-Xclang', '-print-stats'], stderr=PIPE).stderr
      self.assertTextDataContained('*** PCH/Modules Loaded:\nModule: header.h.' + suffix, err)
      # and sanity check it is not mentioned when not
      try_delete('header.h.' + suffix)
      err = run_process([PYTHON, EMCC, 'src.cpp', '-include', 'header.h', '-Xclang', '-print-stats'], stderr=PIPE).stderr
      assert '*** PCH/Modules Loaded:\nModule: header.h.' + suffix not in err.replace('\r\n', '\n'), err

      # with specified target via -o
      try_delete('header.h.' + suffix)
      Popen([PYTHON, EMCC, '-xc++-header', 'header.h', '-o', 'my.' + suffix]).communicate()
      assert os.path.exists('my.' + suffix)

      # -include-pch flag
      Popen([PYTHON, EMCC, '-xc++-header', 'header.h', '-o', 'header.h.' + suffix]).communicate()
      check_execute([PYTHON, EMCC, 'src.cpp', '-include-pch', 'header.h.' + suffix])
      output = run_js('a.out.js')
      assert '|5|' in output, output

  @no_wasm_backend()
  def test_warn_unaligned(self):
    open('src.cpp', 'w').write(r'''
#include <stdio.h>
struct packey {
  char x;
  int y;
  double z;
} __attribute__((__packed__));
int main() {
  volatile packey p;
  p.x = 0;
  p.y = 1;
  p.z = 2;
  return 0;
}
''')
    output = run_process([PYTHON, EMCC, 'src.cpp', '-s', 'WASM=0', '-s', 'WARN_UNALIGNED=1'], stderr=PIPE)
    output = run_process([PYTHON, EMCC, 'src.cpp', '-s', 'WASM=0', '-s', 'WARN_UNALIGNED=1', '-g'], stderr=PIPE)
    assert 'emcc: warning: unaligned store' in output.stderr, output.stderr
    assert 'emcc: warning: unaligned store' in output.stderr, output.stderr
    assert '@line 11 "src.cpp"' in output.stderr, output.stderr

  def test_LEGACY_VM_SUPPORT(self):
    # when modern features are lacking, we can polyfill them or at least warn
    with open('pre.js', 'w') as f: f.write('Math.imul = undefined;')
    def test(expected, opts=[]):
      print(opts)
      result = run_process([PYTHON, EMCC, path_from_root('tests', 'hello_world.c'), '--pre-js', 'pre.js'] + opts, stderr=PIPE, check=False)
      if result.returncode == 0:
        self.assertContained(expected, run_js('a.out.js', stderr=PIPE, full_output=True, engine=NODE_JS, assert_returncode=None))
      else:
        self.assertContained(expected, result.stderr)
    # when legacy is needed, we show an error indicating so
    test('this is a legacy browser, build with LEGACY_VM_SUPPORT')
    # wasm is on by default, and does not mix with legacy, so we show an error
    test('LEGACY_VM_SUPPORT is only supported for asm.js, and not wasm. Build with -s WASM=0', ['-s', 'LEGACY_VM_SUPPORT=1'])
    # legacy + disabling wasm works
    if self.is_wasm_backend():
      return
    test('hello, world!', ['-s', 'LEGACY_VM_SUPPORT=1', '-s', 'WASM=0'])

  def test_on_abort(self):
    expected_output = 'Module.onAbort was called'

    def add_on_abort_and_verify(extra=''):
      with open('a.out.js') as f:
        js = f.read()
      with open('a.out.js', 'w') as f:
        f.write("var Module = { onAbort: function() { console.log('%s') } };\n" % expected_output)
        f.write(extra + '\n')
        f.write(js)
      self.assertContained(expected_output, run_js('a.out.js', assert_returncode=None))

    # test direct abort() C call

    with open('src.c', 'w') as f:
      f.write('''
        #include <stdlib.h>
        int main() {
          abort();
        }
      ''')
    subprocess.check_call([PYTHON, EMCC, 'src.c', '-s', 'BINARYEN_ASYNC_COMPILATION=0'])
    add_on_abort_and_verify()

    # test direct abort() JS call

    with open('src.c', 'w') as f:
      f.write('''
        #include <emscripten.h>
        int main() {
          EM_ASM({ abort() });
        }
      ''')
    subprocess.check_call([PYTHON, EMCC, 'src.c', '-s', 'BINARYEN_ASYNC_COMPILATION=0'])
    add_on_abort_and_verify()

    # test throwing in an abort handler, and catching that

    with open('src.c', 'w') as f:
      f.write('''
        #include <emscripten.h>
        int main() {
          EM_ASM({
            try {
              out('first');
              abort();
            } catch (e) {
              out('second');
              abort();
              throw e;
            }
          });
        }
      ''')
    subprocess.check_call([PYTHON, EMCC, 'src.c', '-s', 'BINARYEN_ASYNC_COMPILATION=0'])
    with open('a.out.js') as f:
      js = f.read()
    with open('a.out.js', 'w') as f:
      f.write("var Module = { onAbort: function() { console.log('%s'); throw 're-throw'; } };\n" % expected_output)
      f.write(js)
    out = run_js('a.out.js', stderr=subprocess.STDOUT, assert_returncode=None)
    print(out)
    self.assertContained(expected_output, out)
    self.assertContained('re-throw', out)
    self.assertContained('first', out)
    self.assertContained('second', out)
    self.assertEqual(out.count(expected_output), 2)

    # test an abort during startup

    subprocess.check_call([PYTHON, EMCC, path_from_root('tests', 'hello_world.c'), '-s', 'WASM=1', '-s', 'BINARYEN_METHOD="interpret-binary"'])
    os.remove('a.out.wasm') # trigger onAbort by intentionally causing startup to fail
    add_on_abort_and_verify()

    # test an abort due to lack of a working binaryen method

    subprocess.check_call([PYTHON, EMCC, path_from_root('tests', 'hello_world.c'), '-s', 'WASM=1', '-s', 'BINARYEN_METHOD="asmjs"'])
    add_on_abort_and_verify(extra='Module.asm = "string-instead-of-code, asmjs method will fail";')

  def test_no_exit_runtime(self):
    open('code.cpp', 'w').write(r'''
#include <stdio.h>

template<int x>
struct Waste {
  Waste() {
    printf("coming around %d\n", x);
  }
  ~Waste() {
    printf("going away %d\n", x);
  }
};

Waste<1> w1;
Waste<2> w2;
Waste<3> w3;
Waste<4> w4;
Waste<5> w5;

int main(int argc, char **argv) {
  return 0;
}
''')

    for wasm in [0, 1]:
      for no_exit in [0, 1]:
        for opts in [[], ['-O1'], ['-O2', '-g2'], ['-O2', '-g2', '--llvm-lto', '1']]:
          print(wasm, no_exit, opts)
          cmd = [PYTHON, EMCC] + opts + ['code.cpp', '-s', 'NO_EXIT_RUNTIME=' + str(no_exit), '-s', 'WASM=' + str(wasm)]
          if wasm:
            cmd += ['--profiling-funcs'] # for function names
          run_process(cmd)
          output = run_js(os.path.join(self.get_dir(), 'a.out.js'), stderr=PIPE, full_output=True, engine=NODE_JS)
          src = open('a.out.js').read()
          if wasm:
            src += '\n' + self.get_wasm_text('a.out.wasm')
          exit = 1-no_exit
          print('  exit:', exit, 'opts:', opts)
          assert 'coming around' in output
          assert ('going away' in output) == exit, 'destructors should not run if no exit'
          assert ('_ZN5WasteILi2EED' in src) == exit, 'destructors should not appear if no exit:\n' + src
          assert ('atexit(' in src) == exit, 'atexit should not appear or be called'

  def test_no_exit_runtime_warnings_flush(self):
    # check we warn if there is unflushed info
    open('code.c', 'w').write(r'''
#include <stdio.h>
int main(int argc, char **argv) {
  printf("hello\n");
  printf("world"); // no newline, not flushed
#if FLUSH
  printf("\n");
#endif
}
''')
    open('code.cpp', 'w').write(r'''
#include <iostream>
int main() {
  using namespace std;
  cout << "hello" << std::endl;
  cout << "world"; // no newline, not flushed
#if FLUSH
  std::cout << std::endl;
#endif
}
''')
    for src in ['code.c', 'code.cpp']:
      for no_exit in [0, 1]:
        for assertions in [0, 1]:
          for flush in [0, 1]:
            # TODO: also check NO_FILESYSTEM here. it never worked though, buffered output was not emitted at shutdown
            print(src, no_exit, assertions, flush)
            cmd = [PYTHON, EMCC, src, '-s', 'NO_EXIT_RUNTIME=%d' % no_exit, '-s', 'ASSERTIONS=%d' % assertions]
            if flush: cmd += ['-DFLUSH']
            subprocess.check_call(cmd)
            output = run_js(os.path.join(self.get_dir(), 'a.out.js'), stderr=PIPE, full_output=True)
            exit = 1-no_exit
            assert 'hello' in output, output
            assert ('world' in output) == (exit or flush), 'unflushed content is shown only when exiting the runtime'
            assert (no_exit and assertions and not flush) == ('stdio streams had content in them that was not flushed. you should set NO_EXIT_RUNTIME to 0' in output), 'warning should be shown'

  def test_no_exit_runtime_warnings_atexit(self):
    open('code.cpp', 'w').write(r'''
#include <stdlib.h>
void bye() {}
int main() {
  atexit(bye);
}
''')
    for no_exit in [0, 1]:
      for assertions in [0, 1]:
        print(no_exit, assertions)
        subprocess.check_call([PYTHON, EMCC, 'code.cpp', '-s', 'NO_EXIT_RUNTIME=%d' % no_exit, '-s', 'ASSERTIONS=%d' % assertions])
        output = run_js(os.path.join(self.get_dir(), 'a.out.js'), stderr=PIPE, full_output=True)
        exit = 1-no_exit
        assert (no_exit and assertions) == ('atexit() called, but NO_EXIT_RUNTIME is set, so atexits() will not be called. set NO_EXIT_RUNTIME to 0' in output), 'warning should be shown'

  def test_fs_after_main(self):
    for args in [[], ['-O1']]:
      print(args)
      run_process([PYTHON, EMCC, path_from_root('tests', 'fs_after_main.cpp')])
      self.assertContained('Test passed.', run_js('a.out.js', engine=NODE_JS))

  @unittest.skipIf('EMCC_DEBUG' in os.environ, 'cannot run in debug mode')
  def test_os_oz(self):
    try:
      os.environ['EMCC_DEBUG'] = '1'
      for args, expect in [
          (['-O1'], 'LLVM opts: -O1'),
          (['-O2'], 'LLVM opts: -O3'),
          (['-Os'], 'LLVM opts: -Os'),
          (['-Oz'], 'LLVM opts: -Oz'),
          (['-O3'], 'LLVM opts: -O3'),
        ]:
        print(args, expect)
        with clean_write_access_to_canonical_temp_dir(self.canonical_temp_dir):
          err = run_process([PYTHON, EMCC, path_from_root('tests', 'hello_world.cpp')] + args, stdout=PIPE, stderr=PIPE).stderr
        self.assertContained(expect, err)
        self.assertContained('hello, world!', run_js('a.out.js'))
    finally:
      del os.environ['EMCC_DEBUG']

  def test_oz_size(self):
    sizes = {}
    for name, args in [
        ('0', ['-o', 'dlmalloc.o']),
        ('1', ['-o', 'dlmalloc.o', '-O1']),
        ('2', ['-o', 'dlmalloc.o', '-O2']),
        ('s', ['-o', 'dlmalloc.o', '-Os']),
        ('z', ['-o', 'dlmalloc.o', '-Oz']),
        ('3', ['-o', 'dlmalloc.o', '-O3']),
        ('0c', ['-c']),
        ('1c', ['-c', '-O1']),
        ('2c', ['-c', '-O2']),
        ('sc', ['-c', '-Os']),
        ('zc', ['-c', '-Oz']),
        ('3c', ['-c', '-O3']),
      ]:
      print(name, args)
      self.clear()
      Popen([PYTHON, EMCC, path_from_root('system', 'lib', 'dlmalloc.c')] + args, stdout=PIPE, stderr=PIPE).communicate()
      sizes[name] = os.stat('dlmalloc.o').st_size
    print(sizes)
    # -c should not affect code size
    for name in ['0', '1', '2', '3', 's', 'z']:
      assert sizes[name] == sizes[name + 'c']
    opt_min = min(sizes['1'], sizes['2'], sizes['3'], sizes['s'], sizes['z'])
    opt_max = max(sizes['1'], sizes['2'], sizes['3'], sizes['s'], sizes['z'])
    assert opt_min - opt_max <= opt_max*0.1, 'opt builds are all fairly close'
    assert sizes['0'] > 1.20*opt_max, 'unopt build is quite larger'

  def test_global_inits(self):
    open('inc.h', 'w').write(r'''
#include <stdio.h>

template<int x>
struct Waste {
  int state;
  Waste() : state(10) {}
  void test(int a) {
    printf("%d\n", a + state);
  }
  ~Waste() {
    printf("going away %d\n", x);
  }
};

Waste<3> *getMore();

''')
    open('main.cpp', 'w').write(r'''
#include "inc.h"

Waste<1> mw1;
Waste<2> mw2;

int main(int argc, char **argv) {
  printf("argc: %d\n", argc);
  mw1.state += argc;
  mw2.state += argc;
  mw1.test(5);
  mw2.test(6);
  getMore()->test(0);
  return 0;
}
''')

    open('side.cpp', 'w').write(r'''
#include "inc.h"

Waste<3> sw3;

Waste<3> *getMore() {
  return &sw3;
}
''')

    for opts, has_global in [
      (['-O2', '-g', '-s', 'NO_EXIT_RUNTIME=0'], True),
      (['-O2', '-g'], False), # no-exit-runtime removes the atexits, and then globalgce can work it's magic to remove the global initializer entirely
      (['-Os', '-g', '-s', 'NO_EXIT_RUNTIME=0'], True),
      (['-Os', '-g'], False),
      (['-O2', '-g', '--llvm-lto', '1', '-s', 'NO_EXIT_RUNTIME=0'], True),
      (['-O2', '-g', '--llvm-lto', '1'], False),
    ]:
      print(opts, has_global)
      Popen([PYTHON, EMCC, 'main.cpp', '-c'] + opts).communicate()
      Popen([PYTHON, EMCC, 'side.cpp', '-c'] + opts).communicate()
      Popen([PYTHON, EMCC, 'main.o', 'side.o'] + opts).communicate()
      output = run_js(os.path.join(self.get_dir(), 'a.out.js'), stderr=PIPE, full_output=True, engine=NODE_JS)
      src = open('a.out.js').read()
      self.assertContained('argc: 1\n16\n17\n10\n', run_js('a.out.js'))
      assert ('_GLOBAL_' in src) == has_global

  def test_implicit_func(self):
    open('src.c', 'w').write(r'''
#include <stdio.h>
int main()
{
    printf("hello %d\n", strnlen("waka", 2)); // Implicit declaration, no header, for strnlen
    int (*my_strnlen)(char*, ...) = strnlen;
    printf("hello %d\n", my_strnlen("shaka", 2));
    return 0;
}
''')

    IMPLICIT_WARNING = '''warning: implicit declaration of function 'strnlen' is invalid in C99'''
    IMPLICIT_ERROR = '''error: implicit declaration of function 'strnlen' is invalid in C99'''

    for opts, expected, compile_expected in [
      ([], None, [IMPLICIT_ERROR]),
      (['-Wno-error=implicit-function-declaration'], ['hello '], [IMPLICIT_WARNING]), # turn error into warning
      (['-Wno-implicit-function-declaration'], ['hello '], []), # turn error into nothing at all (runtime output is incorrect)
    ]:
      print(opts, expected)
      try_delete('a.out.js')
      stderr = run_process([PYTHON, EMCC, 'src.c'] + opts, stderr=PIPE, check=False).stderr
      for ce in compile_expected + ['''warning: incompatible pointer types''']:
        self.assertContained(ce, stderr)
      if expected is None:
        assert not os.path.exists('a.out.js')
      else:
        output = run_js(os.path.join(self.get_dir(), 'a.out.js'), stderr=PIPE, full_output=True)
        for e in expected:
          self.assertContained(e, output)

  def test_incorrect_static_call(self):
    for wasm in [0, 1]:
      for opts in [0, 1]:
        for asserts in [0, 1]:
          extra = []
          if opts != 1-asserts: extra = ['-s', 'ASSERTIONS=' + str(asserts)]
          cmd = [PYTHON, EMCC, path_from_root('tests', 'sillyfuncast2_noasm.ll'), '-O' + str(opts), '-s', 'WASM=' + str(wasm)] + extra
          print(opts, asserts, wasm, cmd)
          stderr = run_process(cmd, stdout=PIPE, stderr=PIPE, check=False).stderr
          assert ('unexpected' in stderr) == asserts, stderr
          assert ("to 'doit'" in stderr) == asserts, stderr

  def test_llvm_lit(self):
    grep_path = Building.which('grep')
    if not grep_path:
      self.skipTest('Skipping other.test_llvm_lit: This test needs the "grep" tool in PATH. If you are using emsdk on Windows, you can obtain it via installing and activating the gnu package.')
    llvm_src = get_fastcomp_src_dir()
    LLVM_LIT = os.path.join(LLVM_ROOT, 'llvm-lit.py')
    if not os.path.exists(LLVM_LIT):
      LLVM_LIT = os.path.join(LLVM_ROOT, 'llvm-lit')
      if not os.path.exists(LLVM_LIT):
        raise Exception('cannot find llvm-lit tool')
    cmd = [PYTHON, LLVM_LIT, '-v', os.path.join(llvm_src, 'test', 'CodeGen', 'JS')]
    print(cmd)
    p = Popen(cmd)
    p.communicate()
    assert p.returncode == 0, 'LLVM tests must pass with exit code 0'

  def test_bad_triple(self):
    # compile a minimal program, with as few dependencies as possible, as
    # native building on CI may not always work well
    with open('minimal.cpp', 'w') as f:
      f.write('int main() { return 0; }')
    run_process([CLANG, 'minimal.cpp', '-c', '-emit-llvm', '-o', 'a.bc'] + get_clang_native_args(), env=get_clang_native_env())
    err = run_process([PYTHON, EMCC, 'a.bc'], stdout=PIPE, stderr=PIPE).stderr
    if self.is_wasm_backend():
      assert 'machine type must be wasm32' in err, err
    else:
      assert 'warning' in err or 'WARNING' in err, err
      assert 'incorrect target triple' in err or 'different target triples' in err, err

  def test_valid_abspath(self):
    # Test whether abspath warning appears
    abs_include_path = os.path.abspath(self.get_dir())
    err = run_process([PYTHON, EMCC, '-I%s' % abs_include_path, '-Wwarn-absolute-paths', path_from_root('tests', 'hello_world.c')], stdout=PIPE, stderr=PIPE).stderr
    warning = '-I or -L of an absolute path "-I%s" encountered. If this is to a local system header/library, it may cause problems (local system files make sense for compiling natively on your system, but not necessarily to JavaScript).' % abs_include_path
    assert(warning in err)

    # Passing an absolute path to a directory inside the emscripten tree is always ok and should not issue a warning.
    abs_include_path = path_from_root('tests')
    err = run_process([PYTHON, EMCC, '-I%s' % abs_include_path, '-Wwarn-absolute-paths', path_from_root('tests', 'hello_world.c')], stdout=PIPE, stderr=PIPE).stderr
    warning = '-I or -L of an absolute path "-I%s" encountered. If this is to a local system header/library, it may cause problems (local system files make sense for compiling natively on your system, but not necessarily to JavaScript).' % abs_include_path
    assert(warning not in err)

    # Hide warning for this include path
    err = run_process([PYTHON, EMCC, '--valid-abspath', abs_include_path,'-I%s' % abs_include_path, '-Wwarn-absolute-paths', path_from_root('tests', 'hello_world.c')], stdout=PIPE, stderr=PIPE).stderr
    assert(warning not in err)

  def test_valid_abspath_2(self):
    if WINDOWS:
      abs_include_path = 'C:\\nowhere\\at\\all'
    else:
      abs_include_path = '/nowhere/at/all'
    cmd = [PYTHON, EMCC, path_from_root('tests', 'hello_world.c'), '--valid-abspath', abs_include_path,'-I%s' % abs_include_path]
    print(' '.join(cmd))
    subprocess.check_call(cmd)
    self.assertContained('hello, world!', run_js('a.out.js'))

  def test_warn_dylibs(self):
    shared_suffixes = ['.so', '.dylib', '.dll']

    for suffix in ['.o', '.a', '.bc', '.so', '.lib', '.dylib', '.js', '.html']:
      err = run_process([PYTHON, EMCC, path_from_root('tests', 'hello_world.c'), '-o', 'out' + suffix], stdout=PIPE, stderr=PIPE).stderr
      warning = 'When Emscripten compiles to a typical native suffix for shared libraries (.so, .dylib, .dll) then it emits an LLVM bitcode file. You should then compile that to an emscripten SIDE_MODULE (using that flag) with suffix .wasm (for wasm) or .js (for asm.js).'
      if suffix in shared_suffixes:
        self.assertContained(warning, err)
      else:
        self.assertNotContained(warning, err)

  def test_side_module_without_proper_target(self):
    # SIDE_MODULE is only meaningful when compiling to wasm (or js+wasm)
    # otherwise, we are just linking bitcode, and should show an error
    for wasm in [0, 1]:
      print(wasm)
      process = run_process([PYTHON, EMCC, path_from_root('tests', 'hello_world.cpp'), '-s', 'SIDE_MODULE=1', '-o', 'a.so', '-s', 'WASM=%d' % wasm], stdout=PIPE, stderr=PIPE, check=False)
      self.assertContained('SIDE_MODULE must only be used when compiling to an executable shared library, and not when emitting LLVM bitcode', process.stderr)
      assert process.returncode is not 0

  @no_wasm_backend()
  def test_simplify_ifs(self):
    def test(src, nums):
      open('src.c', 'w').write(src)
      for opts, ifs in [
        [['-g2'], nums[0]],
        [['--profiling'], nums[1]],
        [['--profiling', '-g2'], nums[2]]
      ]:
        print(opts, ifs)
        if type(ifs) == int: ifs = [ifs]
        try_delete('a.out.js')
        Popen([PYTHON, EMCC, 'src.c', '-O2', '-s', 'WASM=0'] + opts, stdout=PIPE).communicate()
        src = open('a.out.js').read()
        main = src[src.find('function _main'):src.find('\n}', src.find('function _main'))]
        actual_ifs = main.count('if (')
        assert actual_ifs in ifs, main + ' : ' + str([ifs, actual_ifs])
        #print main

    test(r'''
      #include <stdio.h>
      #include <string.h>
      int main(int argc, char **argv) {
        if (argc > 5 && strlen(argv[0]) > 1 && strlen(argv[1]) > 2) printf("halp");
        return 0;
      }
    ''', [3, 1, 1])

    test(r'''
      #include <stdio.h>
      #include <string.h>
      int main(int argc, char **argv) {
        while (argc % 3 == 0) {
          if (argc > 5 && strlen(argv[0]) > 1 && strlen(argv[1]) > 2) {
            printf("halp");
            argc++;
          } else {
            while (argc > 0) {
              printf("%d\n", argc--);
            }
          }
        }
        return 0;
      }
    ''', [8, [5,7], [5,7]])

    test(r'''
      #include <stdio.h>
      #include <string.h>
      int main(int argc, char **argv) {
        while (argc % 17 == 0) argc *= 2;
        if (argc > 5 && strlen(argv[0]) > 10 && strlen(argv[1]) > 20) {
          printf("halp");
          argc++;
        } else {
          printf("%d\n", argc--);
        }
        while (argc % 17 == 0) argc *= 2;
        return argc;
      }
    ''', [6, 3, 3])

    test(r'''
      #include <stdio.h>
      #include <stdlib.h>

      int main(int argc, char *argv[]) {
        if (getenv("A") && getenv("B")) {
            printf("hello world\n");
        } else {
            printf("goodnight moon\n");
        }
        printf("and that's that\n");
        return 0;
      }
    ''', [[3,2], 1, 1])

    test(r'''
      #include <stdio.h>
      #include <stdlib.h>

      int main(int argc, char *argv[]) {
        if (getenv("A") || getenv("B")) {
            printf("hello world\n");
        }
        printf("and that's that\n");
        return 0;
      }
    ''', [[3,2], 1, 1])

  def test_symbol_map(self):
    for m in [0, 1]:
      for wasm in [0, 1]:
        print(m, wasm)
        self.clear()
        cmd = [PYTHON, EMCC, path_from_root('tests', 'hello_world.c'), '-O2']
        if m: cmd += ['--emit-symbol-map']
        if wasm: cmd += ['-s', 'WASM=1']
        print(cmd)
        stderr = run_process(cmd, stderr=PIPE).stderr
        assert (os.path.exists('a.out.js.symbols') == m), stderr
        if m:
          symbols = open('a.out.js.symbols').read()
          assert ':_main' in symbols

  def test_bc_to_bc(self):
    # emcc should 'process' bitcode to bitcode. build systems can request this if
    # e.g. they assume our 'executable' extension is bc, and compile an .o to a .bc
    # (the user would then need to build bc to js of course, but we need to actually
    # emit the bc)
    cmd = Popen([PYTHON, EMCC, '-c', path_from_root('tests', 'hello_world.c')]).communicate()
    assert os.path.exists('hello_world.o')
    cmd = Popen([PYTHON, EMCC, 'hello_world.o', '-o', 'hello_world.bc']).communicate()
    assert os.path.exists('hello_world.o')
    assert os.path.exists('hello_world.bc')

  def test_bad_function_pointer_cast(self):
    open('src.cpp', 'w').write(r'''
#include <stdio.h>

typedef int (*callback) (int, ...);

int impl(int foo) {
  printf("Hello, world.\n");
  return 0;
}

int main() {
  volatile callback f = (callback) impl;
  f(0); /* This fails with or without additional arguments. */
  return 0;
}
''')

    for opts in [0, 1, 2]:
      for safe in [0, 1]:
        for emulate_casts in [0, 1]:
          for emulate_fps in [0, 1]:
            for relocate in [0, 1]:
              for wasm in [0, 1]:
                if self.is_wasm_backend() and not wasm:
                  continue
                cmd = [PYTHON, EMCC, 'src.cpp', '-O' + str(opts), '-s', 'SAFE_HEAP=' + str(safe), '-s', 'WASM=' + str(wasm)]
                if emulate_casts:
                  cmd += ['-s', 'EMULATE_FUNCTION_POINTER_CASTS=1']
                if emulate_fps:
                  cmd += ['-s', 'EMULATED_FUNCTION_POINTERS=1']
                if relocate:
                  cmd += ['-s', 'RELOCATABLE=1'] # disables asm-optimized safe heap
                print(cmd)
                run_process(cmd)
                output = run_js('a.out.js', stderr=PIPE, full_output=True, assert_returncode=None)
                if emulate_casts:
                  # success!
                  self.assertContained('Hello, world.', output)
                else:
                  # otherwise, the error depends on the mode we are in
                  if self.is_wasm_backend() or (wasm and (relocate or emulate_fps)):
                    # wasm trap raised by the vm
                    self.assertContained('function signature mismatch', output)
                  elif safe and not wasm:
                    # non-wasm safe mode checks asm.js function table masks
                    self.assertContained('Function table mask error', output)
                  elif opts == 0:
                    # informative error message (assertions are enabled in -O0)
                    self.assertContained('Invalid function pointer called', output)
                  else:
                    # non-informative abort()
                    self.assertContained('abort(', output)

  @no_wasm_backend()
  def test_aliased_func_pointers(self):
    open('src.cpp', 'w').write(r'''
#include <stdio.h>

int impl1(int foo) { return foo; }
float impla(float foo) { return foo; }
int impl2(int foo) { return foo+1; }
float implb(float foo) { return foo+1; }
int impl3(int foo) { return foo+2; }
float implc(float foo) { return foo+2; }

int main(int argc, char **argv) {
  volatile void *f = (void*)impl1;
  if (argc == 50) f = (void*)impla;
  if (argc == 51) f = (void*)impl2;
  if (argc == 52) f = (void*)implb;
  if (argc == 53) f = (void*)impl3;
  if (argc == 54) f = (void*)implc;
  return (int)f;
}
''')

    print('aliasing')

    sizes_ii = {}
    sizes_dd = {}

    for alias in [None, 0, 1]:
      cmd = [PYTHON, EMCC, 'src.cpp', '-O1', '-s', 'WASM=0']
      if alias is not None:
        cmd += ['-s', 'ALIASING_FUNCTION_POINTERS=' + str(alias)]
      else:
        alias = -1
      print(cmd)
      Popen(cmd).communicate()
      src = open('a.out.js').read().split('\n')
      for line in src:
        if line.strip().startswith('var FUNCTION_TABLE_ii = '):
          sizes_ii[alias] = line.count(',')
        if line.strip().startswith('var FUNCTION_TABLE_dd = '):
          sizes_dd[alias] = line.count(',')

    print('ii', sizes_ii)
    print('dd', sizes_dd)

    for sizes in [sizes_ii, sizes_dd]:
      assert sizes[-1] == sizes[1] # default is to alias
      assert sizes[1] < sizes[0] # without aliasing, we have more unique values and fat tables

  def test_bad_export(self):
    for m in ['', ' ']:
      self.clear()
      cmd = [PYTHON, EMCC, path_from_root('tests', 'hello_world.c'), '-s', 'EXPORTED_FUNCTIONS=["' + m + '_main"]']
      print(cmd)
      stderr = run_process(cmd, stderr=PIPE).stderr
      if m:
        assert 'function requested to be exported, but not implemented: " _main"' in stderr, stderr
      else:
        self.assertContained('hello, world!', run_js('a.out.js'))

  def test_no_dynamic_execution(self):
    run_process([PYTHON, EMCC, path_from_root('tests', 'hello_world.c'), '-O1', '-s', 'NO_DYNAMIC_EXECUTION=1'])
    self.assertContained('hello, world!', run_js('a.out.js'))
    src = open('a.out.js').read()
    assert 'eval(' not in src
    assert 'eval.' not in src
    assert 'new Function' not in src
    try_delete('a.out.js')

    # Test that --preload-file doesn't add an use of eval().
    with open('temp.txt', 'w') as f:
      f.write("foo\n");
    run_process([PYTHON, EMCC, path_from_root('tests', 'hello_world.c'), '-O1',
        '-s', 'NO_DYNAMIC_EXECUTION=1', '--preload-file', 'temp.txt'])
    src = open('a.out.js').read()
    assert 'eval(' not in src
    assert 'eval.' not in src
    assert 'new Function' not in src
    try_delete('a.out.js')

    # Test that -s NO_DYNAMIC_EXECUTION=1 and --closure 1 are not allowed together.
    proc = run_process([PYTHON, EMCC, path_from_root('tests', 'hello_world.c'), '-O1',
        '-s', 'NO_DYNAMIC_EXECUTION=1', '--closure', '1'],
        check=False, stderr=PIPE)
    assert proc.returncode != 0
    try_delete('a.out.js')

    # Test that -s NO_DYNAMIC_EXECUTION=1 and -s RELOCATABLE=1 are not allowed together.
    proc = run_process([PYTHON, EMCC, path_from_root('tests', 'hello_world.c'), '-O1',
        '-s', 'NO_DYNAMIC_EXECUTION=1', '-s', 'RELOCATABLE=1'],
        check=False, stderr=PIPE)
    assert proc.returncode != 0
    try_delete('a.out.js')

    open('test.c', 'w').write(r'''
      #include <emscripten/emscripten.h>
      int main() {
        emscripten_run_script("console.log('hello from script');");
        return 0;
      }
      ''')

    # Test that emscripten_run_script() aborts when -s NO_DYNAMIC_EXECUTION=1
    run_process([PYTHON, EMCC, 'test.c', '-O1', '-s', 'NO_DYNAMIC_EXECUTION=1'])
    self.assertContained('NO_DYNAMIC_EXECUTION=1 was set, cannot eval', run_js(os.path.join(self.get_dir(), 'a.out.js'), assert_returncode=None, full_output=True, stderr=PIPE))
    try_delete('a.out.js')

    # Test that emscripten_run_script() posts a warning when -s NO_DYNAMIC_EXECUTION=2
    run_process([PYTHON, EMCC, 'test.c', '-O1', '-s', 'NO_DYNAMIC_EXECUTION=2'])
    self.assertContained('Warning: NO_DYNAMIC_EXECUTION=2 was set, but calling eval in the following location:', run_js(os.path.join(self.get_dir(), 'a.out.js'), assert_returncode=None, full_output=True, stderr=PIPE))
    self.assertContained('hello from script', run_js(os.path.join(self.get_dir(), 'a.out.js'), assert_returncode=None, full_output=True, stderr=PIPE))
    try_delete('a.out.js')

  def test_init_file_at_offset(self):
    open('src.cpp', 'w').write(r'''
      #include <stdio.h>
      int main() {
        int data = 0x12345678;
        FILE *f = fopen("test.dat", "wb");
        fseek(f, 100, SEEK_CUR);
        fwrite(&data, 4, 1, f);
        fclose(f);

        int data2;
        f = fopen("test.dat", "rb");
        fread(&data2, 4, 1, f); // should read 0s, not that int we wrote at an offset
        printf("read: %d\n", data2);
        fseek(f, 0, SEEK_END);
        long size = ftell(f); // should be 104, not 4
        fclose(f);
        printf("file size is %d\n", size);
      }
    ''')
    Popen([PYTHON, EMCC, 'src.cpp']).communicate()
    self.assertContained('read: 0\nfile size is 104\n', run_js('a.out.js'))

  def test_unlink(self):
    self.do_other_test(os.path.join('other', 'unlink'))

  def test_argv0_node(self):
    open('code.cpp', 'w').write(r'''
#include <stdio.h>
int main(int argc, char **argv) {
  printf("I am %s.\n", argv[0]);
  return 0;
}
''')

    Popen([PYTHON, EMCC, 'code.cpp']).communicate()
    self.assertContained('I am ' + os.path.realpath(self.get_dir()).replace('\\', '/') + '/a.out.js', run_js('a.out.js', engine=NODE_JS).replace('\\', '/'))

  def test_returncode(self):
    open('src.cpp', 'w').write(r'''
      #include <stdio.h>
      #include <stdlib.h>
      int main() {
      #if CALL_EXIT
        exit(CODE);
      #else
        return CODE;
      #endif
      }
    ''')
    for code in [0, 123]:
      for no_exit in [0, 1]:
        for call_exit in [0, 1]:
          for async in [0, 1]:
            subprocess.check_call([PYTHON, EMCC, 'src.cpp', '-DCODE=%d' % code, '-s', 'NO_EXIT_RUNTIME=%d' % no_exit, '-DCALL_EXIT=%d' % call_exit, '-s', 'BINARYEN_ASYNC_COMPILATION=%d' % async])
            for engine in JS_ENGINES:
              if async and engine == V8_ENGINE: continue # async compilation can't return a code in d8
              print(code, no_exit, call_exit, async, engine)
              process = run_process(engine + ['a.out.js'], stdout=PIPE, stderr=PIPE, check=False)
              # we always emit the right exit code, whether we exit the runtime or not
              assert process.returncode == code, [process.returncode, process.stdout, process.stderr]
              assert not process.stdout, process.stdout
              if not call_exit:
                assert not process.stderr, process.stderr
              assert ('but NO_EXIT_RUNTIME is set, so halting execution but not exiting the runtime or preventing further async execution (build with NO_EXIT_RUNTIME=0, if you want a true shutdown)' in process.stderr) == (no_exit and call_exit), process.stderr

  def test_emscripten_force_exit_NO_EXIT_RUNTIME(self):
    open('src.cpp', 'w').write(r'''
      #include <emscripten.h>
      int main() {
      #if CALL_EXIT
        emscripten_force_exit(0);
      #endif
      }
    ''')
    for no_exit in [0, 1]:
      for call_exit in [0, 1]:
        subprocess.check_call([PYTHON, EMCC, 'src.cpp', '-s', 'NO_EXIT_RUNTIME=%d' % no_exit, '-DCALL_EXIT=%d' % call_exit])
        print(no_exit, call_exit)
        out = run_js('a.out.js', stdout=PIPE, stderr=PIPE, full_output=True)
        assert ('emscripten_force_exit cannot actually shut down the runtime, as the build has NO_EXIT_RUNTIME set' in out) == (no_exit and call_exit), out

  def test_mkdir_silly(self):
    open('src.cpp', 'w').write(r'''
#include <stdio.h>
#include <dirent.h>
#include <errno.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <unistd.h>

int main(int argc, char **argv) {
  printf("\n");
  for (int i = 1; i < argc; i++) {
    printf("%d:\n", i);
    int ok = mkdir(argv[i], S_IRWXU|S_IRWXG|S_IRWXO);
    printf("  make %s: %d\n", argv[i], ok);
    DIR *dir = opendir(argv[i]);
    printf("  open %s: %d\n", argv[i], dir != NULL);
    if (dir) {
      struct dirent *entry;
      while ((entry = readdir(dir))) {
        printf("  %s, %d\n", entry->d_name, entry->d_type);
      }
    }
  }
}
    ''')
    Popen([PYTHON, EMCC, 'src.cpp']).communicate()

    # cannot create /, can open
    self.assertContained(r'''
1:
  make /: -1
  open /: 1
  proc, 4
  dev, 4
  home, 4
  tmp, 4
  .., 4
  ., 4
''', run_js('a.out.js', args=['/']))
    # cannot create empty name, cannot open
    self.assertContained(r'''
1:
  make : -1
  open : 0
''', run_js('a.out.js', args=['']))
    # can create unnormalized path, can open
    self.assertContained(r'''
1:
  make /a//: 0
  open /a//: 1
  .., 4
  ., 4
''', run_js('a.out.js', args=['/a//']))
    # can create child unnormalized
    self.assertContained(r'''
1:
  make /a: 0
  open /a: 1
  .., 4
  ., 4
2:
  make /a//b//: 0
  open /a//b//: 1
  .., 4
  ., 4
''', run_js('a.out.js', args=['/a', '/a//b//']))

  def test_stat_silly(self):
    open('src.cpp', 'w').write(r'''
#include <stdio.h>
#include <errno.h>
#include <sys/stat.h>

int main(int argc, char **argv) {
  for (int i = 1; i < argc; i++) {
    const char *path = argv[i];
    struct stat path_stat;
    if (stat(path, &path_stat) != 0) {
      printf("Failed to stat path: %s; errno=%d\n", path, errno);
    } else {
      printf("ok on %s\n", path);
    }
  }
}
    ''')
    Popen([PYTHON, EMCC, 'src.cpp']).communicate()

    # cannot stat ""
    self.assertContained(r'''Failed to stat path: /a; errno=2
Failed to stat path: ; errno=2
''', run_js('a.out.js', args=['/a', '']))

  def test_symlink_silly(self):
    open('src.cpp', 'w').write(r'''
#include <dirent.h>
#include <errno.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <unistd.h>
#include <stdio.h>

int main(int argc, char **argv) {
  if (symlink(argv[1], argv[2]) != 0) {
    printf("Failed to symlink paths: %s, %s; errno=%d\n", argv[1], argv[2], errno);
  } else {
    printf("ok\n");
  }
}
    ''')
    Popen([PYTHON, EMCC, 'src.cpp']).communicate()

    # cannot symlink nonexistents
    self.assertContained(r'''Failed to symlink paths: , abc; errno=2''', run_js('a.out.js', args=['', 'abc']))
    self.assertContained(r'''Failed to symlink paths: , ; errno=2''', run_js('a.out.js', args=['', '']))
    self.assertContained(r'''ok''', run_js('a.out.js', args=['123', 'abc']))
    self.assertContained(r'''Failed to symlink paths: abc, ; errno=2''', run_js('a.out.js', args=['abc', '']))

  def test_rename_silly(self):
    open('src.cpp', 'w').write(r'''
#include <stdio.h>
#include <errno.h>

int main(int argc, char **argv) {
  if (rename(argv[1], argv[2]) != 0) {
    printf("Failed to rename paths: %s, %s; errno=%d\n", argv[1], argv[2], errno);
  } else {
    printf("ok\n");
  }
}
    ''')
    Popen([PYTHON, EMCC, 'src.cpp']).communicate()

    # cannot symlink nonexistents
    self.assertContained(r'''Failed to rename paths: , abc; errno=2''', run_js('a.out.js', args=['', 'abc']))
    self.assertContained(r'''Failed to rename paths: , ; errno=2''', run_js('a.out.js', args=['', '']))
    self.assertContained(r'''Failed to rename paths: 123, abc; errno=2''', run_js('a.out.js', args=['123', 'abc']))
    self.assertContained(r'''Failed to rename paths: abc, ; errno=2''', run_js('a.out.js', args=['abc', '']))

  def test_readdir_r_silly(self):
    open('src.cpp', 'w').write(r'''
#include <iostream>
#include <cstring>
#include <cerrno>
#include <unistd.h>
#include <fcntl.h>
#include <cstdlib>
#include <dirent.h>
#include <sys/stat.h>
#include <sys/types.h>
using std::endl;
namespace
{
  void check(const bool result)
  {
    if(not result) {
      std::cout << "Check failed!" << endl;
      throw "bad";
    }
  }
  // Do a recursive directory listing of the directory whose path is specified
  // by \a name.
  void ls(const std::string& name, std::size_t indent = 0)
  {
    ::DIR *dir;
    struct ::dirent *entry;
    if(indent == 0) {
      std::cout << name << endl;
      ++indent;
    }
    // Make sure we can open the directory.  This should also catch cases where
    // the empty string is passed in.
    if (not (dir = ::opendir(name.c_str()))) {
      const int error = errno;
      std::cout
        << "Failed to open directory: " << name << "; " << error << endl;
      return;
    }
    // Just checking the sanity.
    if (name.empty()) {
      std::cout
        << "Managed to open a directory whose name was the empty string.."
        << endl;
      check(::closedir(dir) != -1);
      return;
    }
    // Iterate over the entries in the directory.
    while ((entry = ::readdir(dir))) {
      const std::string entryName(entry->d_name);
      if (entryName == "." || entryName == "..") {
        // Skip the dot entries.
        continue;
      }
      const std::string indentStr(indent * 2, ' ');
      if (entryName.empty()) {
        std::cout
          << indentStr << "\"\": Found empty string as a "
          << (entry->d_type == DT_DIR ? "directory" : "file")
          << " entry!" << endl;
        continue;
      } else {
        std::cout << indentStr << entryName
                  << (entry->d_type == DT_DIR ? "/" : "") << endl;
      }
      if (entry->d_type == DT_DIR) {
        // We found a subdirectory; recurse.
        ls(std::string(name + (name == "/" ? "" : "/" ) + entryName),
           indent + 1);
      }
    }
    // Close our handle.
    check(::closedir(dir) != -1);
  }
  void touch(const std::string &path)
  {
    const int fd = ::open(path.c_str(), O_CREAT | O_TRUNC, 0644);
    check(fd != -1);
    check(::close(fd) != -1);
  }
}
int main()
{
  check(::mkdir("dir", 0755) == 0);
  touch("dir/a");
  touch("dir/b");
  touch("dir/c");
  touch("dir/d");
  touch("dir/e");
  std::cout << "Before:" << endl;
  ls("dir");
  std::cout << endl;
  // Attempt to delete entries as we walk the (single) directory.
  ::DIR * const dir = ::opendir("dir");
  check(dir != NULL);
  struct ::dirent *entry;
  while((entry = ::readdir(dir)) != NULL) {
    const std::string name(entry->d_name);
    // Skip "." and "..".
    if(name == "." || name == "..") {
      continue;
    }
    // Unlink it.
    std::cout << "Unlinking " << name << endl;
    check(::unlink(("dir/" + name).c_str()) != -1);
  }
  check(::closedir(dir) != -1);
  std::cout << "After:" << endl;
  ls("dir");
  std::cout << endl;
  return 0;
}
    ''')
    Popen([PYTHON, EMCC, 'src.cpp']).communicate()

    # cannot symlink nonexistents
    self.assertContained(r'''Before:
dir
  e
  d
  c
  b
  a

Unlinking e
Unlinking d
Unlinking c
Unlinking b
Unlinking a
After:
dir
''', run_js('a.out.js', args=['', 'abc']))

  def test_emversion(self):
    open('src.cpp', 'w').write(r'''
      #include <stdio.h>
      int main() {
        printf("major: %d\n", __EMSCRIPTEN_major__);
        printf("minor: %d\n", __EMSCRIPTEN_minor__);
        printf("tiny: %d\n", __EMSCRIPTEN_tiny__);
      }
    ''')
    Popen([PYTHON, EMCC, 'src.cpp']).communicate()
    self.assertContained(r'''major: %d
minor: %d
tiny: %d
''' % (EMSCRIPTEN_VERSION_MAJOR, EMSCRIPTEN_VERSION_MINOR, EMSCRIPTEN_VERSION_TINY), run_js('a.out.js'))

  def test_dashE(self):
    open('src.cpp', 'w').write(r'''#include <emscripten.h>
__EMSCRIPTEN_major__ __EMSCRIPTEN_minor__ __EMSCRIPTEN_tiny__ EMSCRIPTEN_KEEPALIVE
''')
    def test(args=[]):
      print(args)
      out = run_process([PYTHON, EMCC, 'src.cpp', '-E'] + args, stdout=PIPE).stdout
      self.assertContained('%d %d %d __attribute__((used))' % (EMSCRIPTEN_VERSION_MAJOR, EMSCRIPTEN_VERSION_MINOR, EMSCRIPTEN_VERSION_TINY), out)
    test()
    test(['--bind'])

  def test_dashE_consistent(self): # issue #3365
    normal = run_process([PYTHON, EMXX, '-v', '-Wwarn-absolute-paths', path_from_root('tests', 'hello_world.cpp'), '-c'], stdout=PIPE, stderr=PIPE).stderr
    dash_e = run_process([PYTHON, EMXX, '-v', '-Wwarn-absolute-paths', path_from_root('tests', 'hello_world.cpp'), '-E'], stdout=PIPE, stderr=PIPE).stderr

    import difflib
    diff = [a.rstrip()+'\n' for a in difflib.unified_diff(normal.split('\n'), dash_e.split('\n'), fromfile='normal', tofile='dash_e')]
    left_std = [x for x in diff if x.startswith('-') and '-std=' in x]
    right_std = [x for x in diff if x.startswith('+') and '-std=' in x]
    assert len(left_std) == len(right_std) == 1, '\n\n'.join(diff)
    bad = [x for x in diff if '-Wwarn-absolute-paths' in x]
    assert len(bad) == 0, '\n\n'.join(diff)

  def test_dashE_respect_dashO(self): # issue #3365
    null_file = 'NUL' if WINDOWS else '/dev/null'
    with_dash_o = run_process([PYTHON, EMXX, path_from_root('tests', 'hello_world.cpp'), '-E', '-o', null_file], stdout=PIPE, stderr=PIPE).stdout
    if WINDOWS: assert not os.path.isfile(null_file)
    without_dash_o = run_process([PYTHON, EMXX, path_from_root('tests', 'hello_world.cpp'), '-E'], stdout=PIPE, stderr=PIPE).stdout
    assert len(with_dash_o) == 0
    assert len(without_dash_o) != 0

  def test_dashM(self):
    out = run_process([PYTHON, EMXX, path_from_root('tests', 'hello_world.cpp'), '-M'], stdout=PIPE).stdout
    self.assertContained('hello_world.o:', out) # Verify output is just a dependency rule instead of bitcode or js

  def test_dashM_consistent(self):
    normal = run_process([PYTHON, EMXX, '-v', '-Wwarn-absolute-paths', path_from_root('tests', 'hello_world.cpp'), '-c'], stdout=PIPE, stderr=PIPE).stderr
    dash_m = run_process([PYTHON, EMXX, '-v', '-Wwarn-absolute-paths', path_from_root('tests', 'hello_world.cpp'), '-M'], stdout=PIPE, stderr=PIPE).stderr

    import difflib
    diff = [a.rstrip()+'\n' for a in difflib.unified_diff(normal.split('\n'), dash_m.split('\n'), fromfile='normal', tofile='dash_m')]
    left_std = [x for x in diff if x.startswith('-') and '-std=' in x]
    right_std = [x for x in diff if x.startswith('+') and '-std=' in x]
    assert len(left_std) == len(right_std) == 1, '\n\n'.join(diff)
    bad = [x for x in diff if '-Wwarn-absolute-paths' in x]
    assert len(bad) == 0, '\n\n'.join(diff)

  def test_dashM_respect_dashO(self):
    null_file = 'NUL' if WINDOWS else '/dev/null'
    with_dash_o = run_process([PYTHON, EMXX, path_from_root('tests', 'hello_world.cpp'), '-M', '-o', null_file], stdout=PIPE, stderr=PIPE).stdout
    if WINDOWS: assert not os.path.isfile(null_file)
    without_dash_o = run_process([PYTHON, EMXX, path_from_root('tests', 'hello_world.cpp'), '-M'], stdout=PIPE, stderr=PIPE).stdout
    assert len(with_dash_o) == 0
    assert len(without_dash_o) != 0

  def test_malloc_implicit(self):
    self.do_other_test(os.path.join('other', 'malloc_implicit'))

  def test_switch64phi(self):
    # issue 2539, fastcomp segfault on phi-i64 interaction
    self.clear()
    open('src.cpp', 'w').write(r'''
#include <cstdint>
#include <limits>
#include <cstdio>

//============================================================================

namespace
{
  class int_adapter {
  public:
    typedef ::int64_t int_type;

    int_adapter(int_type v = 0)
      : value_(v)
    {}
    static const int_adapter pos_infinity()
    {
      return (::std::numeric_limits<int_type>::max)();
    }
    static const int_adapter neg_infinity()
    {
      return (::std::numeric_limits<int_type>::min)();
    }
    static const int_adapter not_a_number()
    {
      return (::std::numeric_limits<int_type>::max)()-1;
    }
    static bool is_neg_inf(int_type v)
    {
      return (v == neg_infinity().as_number());
    }
    static bool is_pos_inf(int_type v)
    {
      return (v == pos_infinity().as_number());
    }
    static bool is_not_a_number(int_type v)
    {
      return (v == not_a_number().as_number());
    }

    bool is_infinity() const
    {
      return (value_ == neg_infinity().as_number() ||
              value_ == pos_infinity().as_number());
    }
    bool is_special() const
    {
      return(is_infinity() || value_ == not_a_number().as_number());
    }
    bool operator<(const int_adapter& rhs) const
    {
      if(value_ == not_a_number().as_number()
         || rhs.value_ == not_a_number().as_number()) {
        return false;
      }
      if(value_ < rhs.value_) return true;
      return false;
    }
    int_type as_number() const
    {
      return value_;
    }

    int_adapter operator-(const int_adapter& rhs)const
    {
      if(is_special() || rhs.is_special())
      {
        if (rhs.is_pos_inf(rhs.as_number()))
        {
          return int_adapter(1);
        }
        if (rhs.is_neg_inf(rhs.as_number()))
        {
          return int_adapter();
        }
      }
      return int_adapter();
    }


  private:
    int_type value_;
  };

  class time_iterator {
  public:
    time_iterator(int_adapter t, int_adapter d)
      : current_(t),
        offset_(d)
    {}

    time_iterator& operator--()
    {
      current_ = int_adapter(current_ - offset_);
      return *this;
    }

    bool operator>=(const int_adapter& t)
    {
      return not (current_ < t);
    }

  private:
    int_adapter current_;
    int_adapter offset_;
  };

  void iterate_backward(const int_adapter *answers, const int_adapter& td)
  {
    int_adapter end = answers[0];
    time_iterator titr(end, td);

    std::puts("");
    for (; titr >= answers[0]; --titr) {
    }
  }
}

int
main()
{
  const int_adapter answer1[] = {};
  iterate_backward(NULL, int_adapter());
  iterate_backward(answer1, int_adapter());
}
    ''')
    Popen([PYTHON, EMCC, 'src.cpp', '-O2', '-s', 'SAFE_HEAP=1']).communicate()
    assert os.path.exists('a.out.js') # build should succeed
    self.assertContained(('trap!', 'segmentation fault loading 4 bytes from address 0'), run_js('a.out.js', assert_returncode=None, stderr=PIPE)) # program should segfault

  def test_only_force_stdlibs(self):
    def test(name):
      print(name)
      Popen([PYTHON, EMXX, path_from_root('tests', 'hello_libcxx.cpp')], stderr=PIPE).communicate()
      self.assertContained('hello, world!', run_js('a.out.js', stderr=PIPE))

    test('normal') # normally is ok

    try:
      os.environ['EMCC_FORCE_STDLIBS'] = 'libc,libcxxabi,libcxx'
      test('forced libs is ok, they were there anyhow')
    finally:
      del os.environ['EMCC_FORCE_STDLIBS']

    try:
      os.environ['EMCC_FORCE_STDLIBS'] = 'libc'
      test('partial list, but ok since we grab them as needed')
    finally:
      del os.environ['EMCC_FORCE_STDLIBS']

    try:
      os.environ['EMCC_FORCE_STDLIBS'] = 'libc'
      os.environ['EMCC_ONLY_FORCED_STDLIBS'] = '1'
      ok = False
      test('fail! not enough stdlibs')
    except:
      ok = True
    finally:
      del os.environ['EMCC_FORCE_STDLIBS']
      del os.environ['EMCC_ONLY_FORCED_STDLIBS']
    assert ok

    try:
      os.environ['EMCC_FORCE_STDLIBS'] = 'libc,libcxxabi,libcxx'
      os.environ['EMCC_ONLY_FORCED_STDLIBS'] = '1'
      test('force all the needed stdlibs, so this works even though we ignore the input file')
    finally:
      del os.environ['EMCC_FORCE_STDLIBS']
      del os.environ['EMCC_ONLY_FORCED_STDLIBS']

  def test_only_force_stdlibs_2(self):
    open('src.cpp', 'w').write(r'''
#include <iostream>
#include <stdexcept>

int main()
{
  try {
    throw std::exception();
    std::cout << "got here" << std::endl;
  }
  catch (const std::exception& ex) {
    std::cout << "Caught exception: " << ex.what() << std::endl;
  }
}
''')
    try:
      os.environ['EMCC_FORCE_STDLIBS'] = 'libc,libcxxabi,libcxx'
      os.environ['EMCC_ONLY_FORCED_STDLIBS'] = '1'
      Popen([PYTHON, EMXX, 'src.cpp', '-s', 'DISABLE_EXCEPTION_CATCHING=0']).communicate()
      self.assertContained('Caught exception: std::exception', run_js('a.out.js', stderr=PIPE))
    finally:
      del os.environ['EMCC_FORCE_STDLIBS']
      del os.environ['EMCC_ONLY_FORCED_STDLIBS']

  def test_strftime_zZ(self):
    open('src.cpp', 'w').write(r'''
#include <cerrno>
#include <cstring>
#include <ctime>
#include <iostream>

int main()
{
  // Buffer to hold the current hour of the day.  Format is HH + nul
  // character.
  char hour[3];

  // Buffer to hold our ISO 8601 formatted UTC offset for the current
  // timezone.  Format is [+-]hhmm + nul character.
  char utcOffset[6];

  // Buffer to hold the timezone name or abbreviation.  Just make it
  // sufficiently large to hold most timezone names.
  char timezone[128];

  std::tm tm;

  // Get the current timestamp.
  const std::time_t now = std::time(NULL);

  // What time is that here?
  if (::localtime_r(&now, &tm) == NULL) {
    const int error = errno;
    std::cout
      << "Failed to get localtime for timestamp=" << now << "; errno=" << error
      << "; " << std::strerror(error) << std::endl;
    return 1;
  }

  size_t result = 0;

  // Get the formatted hour of the day.
  if ((result = std::strftime(hour, 3, "%H", &tm)) != 2) {
    const int error = errno;
    std::cout
      << "Failed to format hour for timestamp=" << now << "; result="
      << result << "; errno=" << error << "; " << std::strerror(error)
      << std::endl;
    return 1;
  }
  std::cout << "The current hour of the day is: " << hour << std::endl;

  // Get the formatted UTC offset in ISO 8601 format.
  if ((result = std::strftime(utcOffset, 6, "%z", &tm)) != 5) {
    const int error = errno;
    std::cout
      << "Failed to format UTC offset for timestamp=" << now << "; result="
      << result << "; errno=" << error << "; " << std::strerror(error)
      << std::endl;
    return 1;
  }
  std::cout << "The current timezone offset is: " << utcOffset << std::endl;

  // Get the formatted timezone name or abbreviation.  We don't know how long
  // this will be, so just expect some data to be written to the buffer.
  if ((result = std::strftime(timezone, 128, "%Z", &tm)) == 0) {
    const int error = errno;
    std::cout
      << "Failed to format timezone for timestamp=" << now << "; result="
      << result << "; errno=" << error << "; " << std::strerror(error)
      << std::endl;
    return 1;
  }
  std::cout << "The current timezone is: " << timezone << std::endl;

  std::cout << "ok!\n";
}
''')
    Popen([PYTHON, EMCC, 'src.cpp']).communicate()
    self.assertContained('ok!', run_js('a.out.js'))

  def test_strptime_symmetry(self):
    Building.emcc(path_from_root('tests','strptime_symmetry.cpp'), output_filename='a.out.js')
    self.assertContained('TEST PASSED', run_js('a.out.js'))

  def test_truncate_from_0(self):
    open('src.cpp', 'w').write(r'''
#include <cerrno>
#include <cstring>
#include <iostream>

#include <fcntl.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <unistd.h>

using std::endl;

//============================================================================
// :: Helpers

namespace
{
  // Returns the size of the regular file specified as 'path'.
  ::off_t getSize(const char* const path)
  {
    // Stat the file and make sure that it's the expected size.
    struct ::stat path_stat;
    if (::stat(path, &path_stat) != 0) {
      const int error = errno;
      std::cout
        << "Failed to lstat path: " << path << "; errno=" << error << "; "
        << std::strerror(error) << endl;
      return -1;
    }

    std::cout
      << "Size of file is: " << path_stat.st_size << endl;
    return path_stat.st_size;
  }

  // Causes the regular file specified in 'path' to have a size of 'length'
  // bytes.
  void resize(const char* const path,
              const ::off_t length)
  {
    std::cout
      << "Truncating file=" << path << " to length=" << length << endl;
    if (::truncate(path, length) == -1)
    {
      const int error = errno;
      std::cout
        << "Failed to truncate file=" << path << "; errno=" << error
        << "; " << std::strerror(error) << endl;
    }

    const ::off_t size = getSize(path);
    if (size != length) {
      std::cout
        << "Failed to truncate file=" << path << " to length=" << length
        << "; got size=" << size << endl;
    }
  }

  // Helper to create a file with the given content.
  void createFile(const std::string& path, const std::string& content)
  {
    std::cout
      << "Creating file: " << path << " with content=" << content << endl;

    const int fd = ::open(path.c_str(), O_CREAT | O_WRONLY, 0644);
    if (fd == -1) {
      const int error = errno;
      std::cout
        << "Failed to open file for writing: " << path << "; errno=" << error
        << "; " << std::strerror(error) << endl;
      return;
    }

    if (::write(fd, content.c_str(), content.size()) != content.size()) {
      const int error = errno;
      std::cout
        << "Failed to write content=" << content << " to file=" << path
        << "; errno=" << error << "; " << std::strerror(error) << endl;

      // Fall through to close FD.
    }

    ::close(fd);
  }
}

//============================================================================
// :: Entry Point
int main()
{
  const char* const file = "/tmp/file";
  createFile(file, "This is some content");
  getSize(file);
  resize(file, 32);
  resize(file, 17);
  resize(file, 0);

  // This throws a JS exception.
  resize(file, 32);
  return 0;
}
''')
    Popen([PYTHON, EMCC, 'src.cpp']).communicate()
    self.assertContained(r'''Creating file: /tmp/file with content=This is some content
Size of file is: 20
Truncating file=/tmp/file to length=32
Size of file is: 32
Truncating file=/tmp/file to length=17
Size of file is: 17
Truncating file=/tmp/file to length=0
Size of file is: 0
Truncating file=/tmp/file to length=32
Size of file is: 32
''', run_js('a.out.js'))

  def test_emcc_s_typo(self):
    # with suggestions
    err = run_process([PYTHON, EMCC, path_from_root('tests', 'hello_world.c'), '-s', 'DISABLE_EXCEPTION_CATCH=1'], stderr=PIPE).stderr
    self.assertContained(r'''Assigning a non-existent settings attribute "DISABLE_EXCEPTION_CATCH"''', err)
    self.assertContained(r'''did you mean one of DISABLE_EXCEPTION_CATCHING?''', err)
    # no suggestions
    err = run_process([PYTHON, EMCC, path_from_root('tests', 'hello_world.c'), '-s', 'CHEEZ=1'], stderr=PIPE).stderr
    self.assertContained(r'''perhaps a typo in emcc's  -s X=Y  notation?''', err)
    self.assertContained(r'''(see src/settings.js for valid values)''', err)

  def test_create_readonly(self):
    open('src.cpp', 'w').write(r'''
#include <cerrno>
#include <cstring>
#include <iostream>

#include <fcntl.h>
#include <unistd.h>

using std::endl;

//============================================================================
// :: Helpers

namespace
{
  // Helper to create a read-only file with content.
  void readOnlyFile(const std::string& path, const std::string& content)
  {
    std::cout
      << "Creating file: " << path << " with content of size="
      << content.size() << endl;

    const int fd = ::open(path.c_str(), O_CREAT | O_WRONLY, 0400);
    if (fd == -1) {
      const int error = errno;
      std::cout
        << "Failed to open file for writing: " << path << "; errno=" << error
        << "; " << std::strerror(error) << endl;
      return;
    }

    // Write the content to the file.
    ssize_t result = 0;
    if ((result = ::write(fd, content.data(), content.size()))
        != ssize_t(content.size()))
    {
      const int error = errno;
      std::cout
        << "Failed to write to file=" << path << "; errno=" << error
        << "; " << std::strerror(error) << endl;
      // Fall through to close the file.
    }
    else {
      std::cout
        << "Data written to file=" << path << "; successfully wrote "
        << result << " bytes" << endl;
    }

    ::close(fd);
  }
}

//============================================================================
// :: Entry Point

int main()
{
  const char* const file = "/tmp/file";
  unlink(file);
  readOnlyFile(file, "This content should get written because the file "
                     "does not yet exist and so, only the mode of the "
                     "containing directory will influence my ability to "
                     "create and open the file. The mode of the file only "
                     "applies to opening of the stream, not subsequent stream "
                     "operations after stream has opened.\n\n");
  readOnlyFile(file, "This should not get written because the file already "
                     "exists and is read-only.\n\n");
}
''')
    Popen([PYTHON, EMCC, 'src.cpp']).communicate()
    self.assertContained(r'''Creating file: /tmp/file with content of size=292
Data written to file=/tmp/file; successfully wrote 292 bytes
Creating file: /tmp/file with content of size=79
Failed to open file for writing: /tmp/file; errno=13; Permission denied
''', run_js('a.out.js'))

  def test_embed_file_large(self):
    # If such long files are encoded on one line,
    # they overflow the interpreter's limit
    large_size = int(1500000)
    open('large.txt', 'w').write('x' * large_size)
    open('src.cpp', 'w').write(r'''
      #include <stdio.h>
      #include <unistd.h>
      int main()
      {
          FILE* fp = fopen("large.txt", "r");
          if (fp) {
              printf("ok\n");
              fseek(fp, 0L, SEEK_END);
              printf("%ld\n", ftell(fp));
          } else {
              printf("failed to open large file.txt\n");
          }
          return 0;
      }
    ''')
    Popen([PYTHON, EMCC, 'src.cpp', '--embed-file', 'large.txt']).communicate()
    for engine in JS_ENGINES:
      if engine == V8_ENGINE: continue # ooms
      print(engine)
      self.assertContained('ok\n' + str(large_size) + '\n', run_js('a.out.js', engine=engine))

  def test_force_exit(self):
    open('src.cpp', 'w').write(r'''
#include <emscripten/emscripten.h>

namespace
{
  extern "C"
  EMSCRIPTEN_KEEPALIVE
  void callback()
  {
    EM_ASM({ out('callback pre()') });
    ::emscripten_force_exit(42);
    EM_ASM({ out('callback post()') });
    }
}

int
main()
{
  EM_ASM({ setTimeout(function() { out("calling callback()"); _callback() }, 100) });
  ::emscripten_exit_with_live_runtime();
  return 123;
}
    ''')
    Popen([PYTHON, EMCC, 'src.cpp']).communicate()
    output = run_js('a.out.js', engine=NODE_JS, assert_returncode=42)
    assert 'callback pre()' in output
    assert 'callback post()' not in output

  def test_bad_locale(self):
    open('src.cpp', 'w').write(r'''

#include <locale.h>
#include <stdio.h>
#include <wctype.h>

int
main(const int argc, const char * const * const argv)
{
  const char * const locale = (argc > 1 ? argv[1] : "C");
  const char * const actual = setlocale(LC_ALL, locale);
  if(actual == NULL) {
    printf("%s locale not supported\n",
           locale);
    return 0;
  }
  printf("locale set to %s: %s\n", locale, actual);
}

    ''')
    Popen([PYTHON, EMCC, 'src.cpp']).communicate()

    self.assertContained('locale set to C: C;C;C;C;C;C',
                         run_js('a.out.js', args=['C']))
    self.assertContained('locale set to waka: waka;waka;waka;waka;waka;waka',
                         run_js('a.out.js', args=['waka']))

  def test_js_main(self):
    # try to add a main() from JS, at runtime. this is not supported (the
    # compiler needs to know at compile time about main).
    open('pre_main.js', 'w').write(r'''
      var Module = {
        '_main': function() {
        }
      };
    ''')
    open('src.cpp', 'w').write('')
    subprocess.check_call([PYTHON, EMCC, 'src.cpp', '--pre-js', 'pre_main.js'])
    self.assertContained('compiled without a main, but one is present. if you added it from JS, use Module["onRuntimeInitialized"]',
                         run_js('a.out.js', assert_returncode=None, stderr=PIPE))

  def test_js_malloc(self):
    open('src.cpp', 'w').write(r'''
#include <stdio.h>
#include <emscripten.h>

int main() {
  EM_ASM({
    for (var i = 0; i < 1000; i++) {
      var ptr = Module._malloc(1024*1024); // only done in JS, but still must not leak
      Module._free(ptr);
    }
  });
  printf("ok.\n");
}
    ''')
    Popen([PYTHON, EMCC, 'src.cpp']).communicate()
    self.assertContained('ok.', run_js('a.out.js', args=['C']))

  def test_locale_wrong(self):
    open('src.cpp', 'w').write(r'''
#include <locale>
#include <iostream>
#include <stdexcept>

int
main(const int argc, const char * const * const argv)
{
  const char * const name = argc > 1 ? argv[1] : "C";

  try {
    const std::locale locale(name);
    std::cout
      << "Constructed locale \"" << name << "\"\n"
      << "This locale is "
      << (locale == std::locale::global(locale) ? "" : "not ")
      << "the global locale.\n"
      << "This locale is " << (locale == std::locale::classic() ? "" : "not ")
      << "the C locale." << std::endl;

  } catch(const std::runtime_error &ex) {
    std::cout
      << "Can't construct locale \"" << name << "\": " << ex.what()
      << std::endl;
    return 1;

  } catch(...) {
    std::cout
      << "FAIL: Unexpected exception constructing locale \"" << name << '\"'
      << std::endl;
    return 127;
  }
}
    ''')
    Popen([PYTHON, EMCC, 'src.cpp', '-s', 'NO_EXIT_RUNTIME=0', '-s', 'DISABLE_EXCEPTION_CATCHING=0']).communicate()
    self.assertContained('Constructed locale "C"\nThis locale is the global locale.\nThis locale is the C locale.', run_js('a.out.js', args=['C']))
    self.assertContained('''Can't construct locale "waka": collate_byname<char>::collate_byname failed to construct for waka''', run_js('a.out.js', args=['waka'], assert_returncode=1))

  def test_cleanup_os(self):
    # issue 2644
    def test(args, be_clean):
      print(args)
      self.clear()
      shutil.copyfile(path_from_root('tests', 'hello_world.c'), 'a.c')
      open('b.c', 'w').write(' ')
      Popen([PYTHON, EMCC, 'a.c', 'b.c'] + args).communicate()
      clutter = glob.glob('*.o')
      if be_clean: assert len(clutter) == 0, 'should not leave clutter ' + str(clutter)
      else: assert len(clutter) == 2, 'should leave .o files'
    test(['-o', 'c.bc'], True)
    test(['-o', 'c.js'], True)
    test(['-o', 'c.html'], True)
    test(['-c'], False)

  @no_wasm_backend()
  def test_js_dash_g(self):
    open('src.c', 'w').write('''
      #include <stdio.h>
      #include <assert.h>

      void checker(int x) {
        x += 20;
        assert(x < 15); // this is line 7!
      }

      int main() {
        checker(10);
        return 0;
      }
    ''')

    def check(has):
      print(has)
      lines = open('a.out.js', 'r').readlines()
      lines = [line for line in lines if '___assert_fail(' in line or '___assert_func(' in line]
      found_line_num = any(('//@line 7 "' in line) for line in lines)
      found_filename = any(('src.c"\n' in line) for line in lines)
      assert found_line_num == has, 'Must have debug info with the line number'
      assert found_filename == has, 'Must have debug info with the filename'

    Popen([PYTHON, EMCC, '-s', 'WASM=0', 'src.c', '-g']).communicate()
    check(True)
    Popen([PYTHON, EMCC, '-s', 'WASM=0', 'src.c']).communicate()
    check(False)
    Popen([PYTHON, EMCC, '-s', 'WASM=0', 'src.c', '-g0']).communicate()
    check(False)
    Popen([PYTHON, EMCC, '-s', 'WASM=0', 'src.c', '-g0', '-g']).communicate() # later one overrides
    check(True)
    Popen([PYTHON, EMCC, '-s', 'WASM=0', 'src.c', '-g', '-g0']).communicate() # later one overrides
    check(False)

  def test_dash_g_bc(self):
    def test(opts):
      print(opts)
      def get_size(name):
        return os.stat(name).st_size
      Popen([PYTHON, EMCC, path_from_root('tests', 'hello_world.c'), '-o', 'a_.bc'] + opts).communicate()
      sizes = { '_': get_size('a_.bc') }
      Popen([PYTHON, EMCC, path_from_root('tests', 'hello_world.c'), '-g', '-o', 'ag.bc'] + opts).communicate()
      sizes['g'] = get_size('ag.bc')
      for i in range(0, 5):
        Popen([PYTHON, EMCC, path_from_root('tests', 'hello_world.c'), '-g' + str(i), '-o', 'a' + str(i) + '.bc'] + opts).communicate()
        sizes[i] = get_size('a' + str(i) + '.bc')
      print('  ', sizes)
      assert sizes['_'] == sizes[0] == sizes[1] == sizes[2] == sizes[3], 'no debug or <4 debug, means no llvm debug info ' + str(sizes)
      assert sizes['g'] == sizes[4], '-g or -g4 means llvm debug info ' + str(sizes)
      assert sizes['_'] < sizes['g'], 'llvm debug info has positive size ' + str(sizes)
    test([])
    test(['-O1'])

  def test_no_filesystem(self):
    FS_MARKER = 'var FS'
    # fopen forces full filesystem support
    check_execute([PYTHON, EMCC, path_from_root('tests', 'hello_world_fopen.c'), '-s', 'ERROR_ON_UNDEFINED_SYMBOLS=1'])
    yes_size = os.stat('a.out.js').st_size
    self.assertContained('hello, world!', run_js('a.out.js'))
    assert FS_MARKER in open('a.out.js').read()
    check_execute([PYTHON, EMCC, path_from_root('tests', 'hello_world.c'), '-s', 'ERROR_ON_UNDEFINED_SYMBOLS=1'])
    no_size = os.stat('a.out.js').st_size
    self.assertContained('hello, world!', run_js('a.out.js'))
    assert FS_MARKER not in open('a.out.js').read()
    print('yes fs, no fs:', yes_size, no_size)
    assert yes_size - no_size > 100000 # 100K of FS code is removed
    assert no_size < 360000

  def test_no_nuthin(self):
    # check NO_FILESYSTEM is automatically set, and effective
    def test(opts, absolute):
      print('opts, absolute:', opts, absolute)
      sizes = {}
      def do(name, source, moar_opts):
        self.clear()
        # pad the name to a common length so that doesn't effect the size of the
        # output
        padded_name = name + '_' * (20 - len(name))
        run_process([PYTHON, EMCC, path_from_root('tests', source), '-o', padded_name + '.js'] + opts + moar_opts)
        sizes[name] = os.path.getsize(padded_name + '.js')
        if os.path.exists(padded_name + '.wasm'):
          sizes[name] += os.path.getsize(padded_name + '.wasm')
        self.assertContained('hello, world!', run_js(padded_name + '.js'))
      do('normal', 'hello_world_fopen.c', [])
      do('no_fs', 'hello_world.c', []) # without fopen, we should auto-detect we do not need full fs support and can do NO_FILESYSTEM
      do('no_fs_manual', 'hello_world.c', ['-s', 'NO_FILESYSTEM=1'])
      print('  ', sizes)
      self.assertLess(sizes['no_fs'], sizes['normal'])
      self.assertLess(sizes['no_fs'], absolute)
      # manual can usually remove a tiny bit more
      self.assertLess(sizes['no_fs_manual'], sizes['no_fs'] + 30)
    test(['-s', 'ASSERTIONS=0'], 120000) # we don't care about code size with assertions
    test(['-O1'], 90000)
    test(['-O2'], 45000)
    test(['-O3', '--closure', '1'], 17000)
    # asm.js too
    if not self.is_wasm_backend():
      test(['-O3', '--closure', '1', '-s', 'WASM=0'], 36000)
      test(['-O3', '--closure', '2', '-s', 'WASM=0'], 33000) # might change now and then

  def test_no_browser(self):
    BROWSER_INIT = 'var Browser'

    check_execute([PYTHON, EMCC, path_from_root('tests', 'hello_world.c')])
    assert BROWSER_INIT not in open('a.out.js').read()

    check_execute([PYTHON, EMCC, path_from_root('tests', 'browser_main_loop.c')]) # uses emscripten_set_main_loop, which needs Browser
    assert BROWSER_INIT in open('a.out.js').read()

  def test_EXPORTED_RUNTIME_METHODS(self):
    def test(opts, has, not_has):
      print(opts, has, not_has)
      self.clear()
      # check without assertions, as with assertions we add stubs for the things we remove (which
      # print nice error messages)
      Popen([PYTHON, EMCC, path_from_root('tests', 'hello_world.c'), '-s', 'ASSERTIONS=0'] + opts).communicate()
      self.assertContained('hello, world!', run_js('a.out.js'))
      src = open('a.out.js').read()
      self.assertContained(has, src)
      self.assertNotContained(not_has, src)

    test([], 'Module["', 'Module["waka')
    test(['-s', 'EXPORTED_RUNTIME_METHODS=[]'], '', 'Module["addRunDependency')
    test(['-s', 'EXPORTED_RUNTIME_METHODS=["addRunDependency"]'], 'Module["addRunDependency', 'Module["waka')
    test(['-s', 'EXPORTED_RUNTIME_METHODS=[]', '-s', 'EXTRA_EXPORTED_RUNTIME_METHODS=["addRunDependency"]'], 'Module["addRunDependency', 'Module["waka')

  def test_stat_fail_alongtheway(self):
    open('src.cpp', 'w').write(r'''
#include <errno.h>
#include <stdio.h>
#include <sys/types.h>
#include <sys/stat.h>
#include <unistd.h>
#include <stdlib.h>
#include <fcntl.h>
#include <string.h>

#define CHECK(expression) \
  if(!(expression)) {                            \
    error = errno;                               \
    printf("FAIL: %s\n", #expression); fail = 1; \
  } else {                                       \
    error = errno;                               \
    printf("pass: %s\n", #expression);           \
  }                                              \

int
main()
{
  int error;
  int fail = 0;
  CHECK(mkdir("path", 0777) == 0);
  CHECK(close(open("path/file", O_CREAT | O_WRONLY, 0644)) == 0);
  {
    struct stat st;
    CHECK(stat("path", &st) == 0);
    CHECK(st.st_mode = 0777);
  }
  {
    struct stat st;
    CHECK(stat("path/nosuchfile", &st) == -1);
    printf("info: errno=%d %s\n", error, strerror(error));
    CHECK(error == ENOENT);
  }
  {
    struct stat st;
    CHECK(stat("path/file", &st) == 0);
    CHECK(st.st_mode = 0666);
  }
  {
    struct stat st;
    CHECK(stat("path/file/impossible", &st) == -1);
    printf("info: errno=%d %s\n", error, strerror(error));
    CHECK(error == ENOTDIR);
  }
  {
    struct stat st;
    CHECK(lstat("path/file/impossible", &st) == -1);
    printf("info: errno=%d %s\n", error, strerror(error));
    CHECK(error == ENOTDIR);
  }
  return fail;
}
''')
    Popen([PYTHON, EMCC, 'src.cpp']).communicate()
    self.assertContained(r'''pass: mkdir("path", 0777) == 0
pass: close(open("path/file", O_CREAT | O_WRONLY, 0644)) == 0
pass: stat("path", &st) == 0
pass: st.st_mode = 0777
pass: stat("path/nosuchfile", &st) == -1
info: errno=2 No such file or directory
pass: error == ENOENT
pass: stat("path/file", &st) == 0
pass: st.st_mode = 0666
pass: stat("path/file/impossible", &st) == -1
info: errno=20 Not a directory
pass: error == ENOTDIR
pass: lstat("path/file/impossible", &st) == -1
info: errno=20 Not a directory
pass: error == ENOTDIR
''', run_js('a.out.js'))


  @no_wasm_backend()
  def test_emterpreter(self):
    if SPIDERMONKEY_ENGINE not in JS_ENGINES: self.skipTest('test_emterpreter requires SpiderMonkey to run.')

    def do_emcc_test(source, args, output, emcc_args=[]):
      print()
      print('emcc', source[:40], '\n' in source)
      try_delete('a.out.js')
      if '\n' in source:
        open('src.cpp', 'w').write(source)
        source = 'src.cpp'
      else:
        source = path_from_root('tests', source)
      Popen([PYTHON, EMCC, source, '-O2', '-s', 'EMTERPRETIFY=1', '-g2', '-s', 'WASM=0'] + emcc_args).communicate()
      self.assertTextDataContained(output, run_js('a.out.js', args=args))
      out = run_js('a.out.js', engine=SPIDERMONKEY_ENGINE, args=args, stderr=PIPE, full_output=True)
      self.assertTextDataContained(output, out)
      self.validate_asmjs(out)
      # -g2 enables these
      src = open('a.out.js').read()
      assert 'function emterpret' in src, 'emterpreter should exist'
      # and removing calls to the emterpreter break, so it was being used
      out1 = run_js('a.out.js', args=args)
      assert output in out1
      open('a.out.js', 'w').write(src.replace('function emterpret', 'function do_not_find_me'))
      out2 = run_js('a.out.js', args=args, stderr=PIPE, assert_returncode=None)
      assert output not in out2, out2
      assert out1 != out2

    def do_test(source, args, output):
      print()
      print('emcc', source.replace('\n', '.')[:40], '\n' in source)
      self.clear()
      if '\n' in source:
        open('src.cpp', 'w').write(source)
        source = 'src.cpp'
      else:
        source = path_from_root('tests', source)
      Popen([PYTHON, EMCC, source, '-O2', '--profiling', '-s', 'FINALIZE_ASM_JS=0', '-s', 'GLOBAL_BASE=2048', '-s', 'ALLOW_MEMORY_GROWTH=0', '-s', 'WASM=0']).communicate()
      Popen([PYTHON, path_from_root('tools', 'emterpretify.py'), 'a.out.js', 'em.out.js', 'ASYNC=0']).communicate()
      self.assertTextDataContained(output, run_js('a.out.js', args=args))
      self.assertTextDataContained(output, run_js('em.out.js', args=args))
      out = run_js('em.out.js', engine=SPIDERMONKEY_ENGINE, args=args, stderr=PIPE, full_output=True)
      self.assertTextDataContained(output, out)
      self.validate_asmjs(out)

    # generate default shell for js test
    def make_default(args=[]):
      Popen([PYTHON, EMCC, path_from_root('tests', 'hello_world.c'), '-O2', '--profiling', '-s', 'FINALIZE_ASM_JS=0', '-s', 'GLOBAL_BASE=2048', '-s', 'WASM=0'] + args).communicate()
      default = open('a.out.js').read()
      start = default.index('function _main(')
      end = default.index('}', start)
      default = default[:start] + '{{{MAIN}}}' + default[end+1:]
      default_mem = open('a.out.js.mem', 'rb').read()
      return default, default_mem
    default, default_mem = make_default()
    default_float, default_float_mem = make_default(['-s', 'PRECISE_F32=1'])

    def do_js_test(name, source, args, output, floaty=False):
      print()
      print('js', name)
      self.clear()
      if '\n' not in source:
        source = open(source).read()
      the_default = default if not floaty else default_float
      the_default_mem = default_mem if not floaty else default_float_mem
      source = the_default.replace('{{{MAIN}}}', source)
      open('a.out.js', 'w').write(source)
      open('a.out.js.mem', 'wb').write(the_default_mem)
      Popen([PYTHON, path_from_root('tools', 'emterpretify.py'), 'a.out.js', 'em.out.js', 'ASYNC=0']).communicate()
      sm_no_warn = [x for x in SPIDERMONKEY_ENGINE if x != '-w']
      self.assertTextDataContained(output, run_js('a.out.js', engine=sm_no_warn, args=args)) # run in spidermonkey for print()
      self.assertTextDataContained(output, run_js('em.out.js', engine=sm_no_warn, args=args))

    do_emcc_test('hello_world.c', [], 'hello, world!')

    do_test('hello_world.c', [], 'hello, world!')
    do_test('hello_world_loop.cpp', [], 'hello, world!')
    do_test('fannkuch.cpp', ['5'], 'Pfannkuchen(5) = 7.')

    print('profiling')

    do_emcc_test('fannkuch.cpp', ['5'], 'Pfannkuchen(5) = 7.', ['-g2'])
    normal = open('a.out.js').read()
    shutil.copyfile('a.out.js', 'last.js')
    do_emcc_test('fannkuch.cpp', ['5'], 'Pfannkuchen(5) = 7.', ['-g2', '--profiling'])
    profiling = open('a.out.js').read()
    assert len(profiling) > len(normal) + 250, [len(profiling), len(normal)] # should be much larger

    print('blacklisting')

    do_emcc_test('fannkuch.cpp', ['5'], 'Pfannkuchen(5) = 7.', [])
    src = open('a.out.js').read()
    assert 'emterpret' in self.get_func(src, '_main'), 'main is emterpreted'
    assert 'function _atoi(' not in src, 'atoi is emterpreted and does not even have a trampoline, since only other emterpreted can reach it'

    do_emcc_test('fannkuch.cpp', ['5'], 'Pfannkuchen(5) = 7.', ['-s', 'EMTERPRETIFY_BLACKLIST=["_main"]']) # blacklist main
    src = open('a.out.js').read()
    assert 'emterpret' not in self.get_func(src, '_main'), 'main is NOT emterpreted, it was  blacklisted'
    assert 'emterpret' in self.get_func(src, '_atoi'), 'atoi is emterpreted'

    do_emcc_test('fannkuch.cpp', ['5'], 'Pfannkuchen(5) = 7.', ['-s', 'EMTERPRETIFY_BLACKLIST=["_main", "_atoi"]']) # blacklist main and atoi
    src = open('a.out.js').read()
    assert 'emterpret' not in self.get_func(src, '_main'), 'main is NOT emterpreted, it was  blacklisted'
    assert 'emterpret' not in self.get_func(src, '_atoi'), 'atoi is NOT emterpreted either'

    open('blacklist.txt', 'w').write('["_main", "_atoi"]')
    do_emcc_test('fannkuch.cpp', ['5'], 'Pfannkuchen(5) = 7.', ['-s', 'EMTERPRETIFY_BLACKLIST=@blacklist.txt']) # blacklist main and atoi with a @response file
    src = open('a.out.js').read()
    assert 'emterpret' not in self.get_func(src, '_main'), 'main is NOT emterpreted, it was  blacklisted'
    assert 'emterpret' not in self.get_func(src, '_atoi'), 'atoi is NOT emterpreted either'

    print('whitelisting')

    do_emcc_test('fannkuch.cpp', ['5'], 'Pfannkuchen(5) = 7.', ['-s', 'EMTERPRETIFY_WHITELIST=[]'])
    src = open('a.out.js').read()
    assert 'emterpret' in self.get_func(src, '_main'), 'main is emterpreted'
    assert 'function _atoi(' not in src, 'atoi is emterpreted and does not even have a trampoline, since only other emterpreted can reach it'

    do_emcc_test('fannkuch.cpp', ['5'], 'Pfannkuchen(5) = 7.', ['-s', 'EMTERPRETIFY_WHITELIST=["_main"]'])
    src = open('a.out.js').read()
    assert 'emterpret' in self.get_func(src, '_main')
    assert 'emterpret' not in self.get_func(src, '_atoi'), 'atoi is not in whitelist, so it is not emterpreted'

    do_emcc_test('fannkuch.cpp', ['5'], 'Pfannkuchen(5) = 7.', ['-s', 'EMTERPRETIFY_WHITELIST=["_main", "_atoi"]'])
    src = open('a.out.js').read()
    assert 'emterpret' in self.get_func(src, '_main')
    assert 'function _atoi(' not in src, 'atoi is emterpreted and does not even have a trampoline, since only other emterpreted can reach it'

    open('whitelist.txt', 'w').write('["_main"]')
    do_emcc_test('fannkuch.cpp', ['5'], 'Pfannkuchen(5) = 7.', ['-s', 'EMTERPRETIFY_WHITELIST=@whitelist.txt'])
    src = open('a.out.js').read()
    assert 'emterpret' in self.get_func(src, '_main')
    assert 'emterpret' not in self.get_func(src, '_atoi'), 'atoi is not in whitelist, so it is not emterpreted'

    do_test(r'''
#include <stdio.h>

int main() {
  volatile float f;
  volatile float *ff = &f;
  *ff = -10;
  printf("hello, world! %d\n", (int)f);
  return 0;
}
''', [], 'hello, world! -10')

    do_test(r'''
#include <stdio.h>

int main() {
  volatile float f;
  volatile float *ff = &f;
  *ff = -10;
  printf("hello, world! %.2f\n", f);
  return 0;
}
''', [], 'hello, world! -10.00')

    do_js_test('float', r'''
function _main() {
  var f = f0;
  f = f0 + f0;
  print(f);
}
''', [], '0\n', floaty=True)

    do_js_test('conditionals', r'''
function _main() {
 var i8 = 0;
 var d10 = +d10, d11 = +d11, d7 = +d7, d5 = +d5, d6 = +d6, d9 = +d9;
 d11 = +1;
 d7 = +2;
 d5 = +3;
 d6 = +4;
 d10 = d11 < d7 ? d11 : d7;
 print(d10);
 d9 = d5 < d6 ? d5 : d6;
 print(d9);
 HEAPF64[tempDoublePtr >> 3] = d10;
 i8 = STACKTOP;
 HEAP32[i8 >> 2] = HEAP32[tempDoublePtr >> 2];
 HEAP32[i8 + 4 >> 2] = HEAP32[tempDoublePtr + 4 >> 2];
 print(HEAP32[i8 >> 2]);
 print(HEAP32[i8 + 4 >> 2]);
}
''', [], '1\n3\n0\n1072693248\n')

    do_js_test('bigswitch', r'''
function _main() {
 var i2 = 0, i3 = 0, i4 = 0, i6 = 0, i8 = 0, i9 = 0, i10 = 0, i11 = 0, i12 = 0, i13 = 0, i14 = 0, i15 = 0, i16 = 0, i5 = 0, i7 = 0, i1 = 0;
 print(4278);
 i6 = 0;
 L1 : while (1) {
  i11 = -1;
  switch ((i11 | 0)) {
  case 0:
   {
    i6 = 67;
    break;
   }
  default:
   {}
  }
  print(i6);
  break;
 }
 print(i6);
}
''', [], '4278\n0\n0\n')

    do_js_test('big int compare', r'''
function _main() {
  print ((0 > 4294963001) | 0);
}
''', [], '0\n')

    do_js_test('effectless expressions, with a subeffect', r'''
function _main() {
  (print (123) | 0) != 0;
  print (456) | 0;
  0 != (print (789) | 0);
  0 | (print (159) | 0);
}
''', [], '123\n456\n789\n159\n')

    do_js_test('effectless unary', r'''
function _main() {
  !(0 != 0);
  !(print (123) | 0);
}
''', [], '123\n')

    do_js_test('flexible mod', r'''
function _main() {
  print(1 % 16);
}
''', [], '1\n')

    # codegen log tests

    def do_log_test(source, expected, func):
      print('log test', source, expected)
      try:
        os.environ['EMCC_LOG_EMTERPRETER_CODE'] = '1'
        err = run_process([PYTHON, EMCC, source, '-O3', '-s', 'EMTERPRETIFY=1'], stderr=PIPE).stderr
      finally:
        del os.environ['EMCC_LOG_EMTERPRETER_CODE']
      lines = err.split('\n')
      lines = [line for line in lines if 'raw bytecode for ' + func in line]
      assert len(lines) == 1, '\n\n'.join(lines)
      err = lines[0]
      parts = err.split('insts: ')
      pre, post = parts[:2]
      assert func in pre, pre
      post = post.split('\n')[0]
      seen = int(post)
      print('  seen', seen, ', expected ', expected, type(seen), type(expected))
      assert expected == seen or (type(expected) in [list, tuple] and seen in expected), ['expect', expected, 'but see', seen]

    do_log_test(path_from_root('tests', 'primes.cpp'), list(range(88, 101)), '_main')
    do_log_test(path_from_root('tests', 'fannkuch.cpp'), list(range(226, 241)), '__Z15fannkuch_workerPv')

  def test_emterpreter_advise(self):
    out = run_process([PYTHON, EMCC, path_from_root('tests', 'emterpreter_advise.cpp'), '-s', 'EMTERPRETIFY=1', '-s', 'EMTERPRETIFY_ASYNC=1', '-s', 'EMTERPRETIFY_ADVISE=1'], stdout=PIPE).stdout
    self.assertContained('-s EMTERPRETIFY_WHITELIST=\'["__Z6middlev", "__Z7sleeperv", "__Z8recurserv", "_main"]\'', out)

    out = run_process([PYTHON, EMCC, path_from_root('tests', 'emterpreter_advise_funcptr.cpp'), '-s', 'EMTERPRETIFY=1', '-s', 'EMTERPRETIFY_ASYNC=1', '-s', 'EMTERPRETIFY_ADVISE=1'], stdout=PIPE).stdout
    self.assertContained('-s EMTERPRETIFY_WHITELIST=\'["__Z4posti", "__Z5post2i", "__Z6middlev", "__Z7sleeperv", "__Z8recurserv", "_main"]\'', out)

    out = run_process([PYTHON, EMCC, path_from_root('tests', 'emterpreter_advise_synclist.c'), '-s', 'EMTERPRETIFY=1', '-s', 'EMTERPRETIFY_ASYNC=1', '-s', 'EMTERPRETIFY_ADVISE=1', '-s', 'EMTERPRETIFY_SYNCLIST=["_j","_k"]'], stdout=PIPE).stdout
    self.assertContained('-s EMTERPRETIFY_WHITELIST=\'["_a", "_b", "_e", "_f", "_main"]\'', out)

    # The same EMTERPRETIFY_WHITELIST should be in core.test_coroutine_emterpretify_async
    out = run_process([PYTHON, EMCC, path_from_root('tests', 'test_coroutines.cpp'), '-s', 'EMTERPRETIFY=1', '-s', 'EMTERPRETIFY_ASYNC=1', '-s', 'EMTERPRETIFY_ADVISE=1'], stdout=PIPE).stdout
    self.assertContained('-s EMTERPRETIFY_WHITELIST=\'["_f", "_fib", "_g"]\'', out)

  def test_emterpreter_async_assertions(self):
    # emterpretify-async mode with assertions adds checks on each call out of the emterpreter;
    # make sure we handle all possible types there
    for t, out in [
      ('int',    '18.00'),
      ('float',  '18.51'),
      ('double', '18.51'),
    ]:
      print(t, out)
      open('src.c', 'w').write(r'''
        #include <stdio.h>
        #include <emscripten.h>

        #define TYPE %s

        TYPE marfoosh(TYPE input) {
          return input * 1.5;
        }

        TYPE fleefl(TYPE input) {
          return marfoosh(input);
        }

        int main(void) {
          printf("result: %%.2f\n", (double)fleefl((TYPE)12.34));
        }
      ''' % t)
      run_process([PYTHON, EMCC, 'src.c', '-s', 'EMTERPRETIFY=1', '-s', 'EMTERPRETIFY_ASYNC=1', '-s', 'EMTERPRETIFY_WHITELIST=["_fleefl"]', '-s', 'PRECISE_F32=1'])
      self.assertContained('result: ' + out, run_js('a.out.js'))

  def test_call_nonemterpreted_during_sleep(self):
    open('src.c', 'w').write(r'''
#include <stdio.h>
#include <emscripten.h>

EMSCRIPTEN_KEEPALIVE void emterpreted_yielder() {
  int counter = 0;
  while (1) {
    printf("emterpreted_yielder() sleeping...\n");
    emscripten_sleep_with_yield(10);
    counter++;
    if (counter == 3) {
      printf("Success\n");
      break;
    }
  }
}

EMSCRIPTEN_KEEPALIVE void not_emterpreted() {
  printf("Entering not_emterpreted()\n");
}

int main() {
  EM_ASM({
    setTimeout(function () {
      console.log("calling not_emterpreted()");
      Module["_not_emterpreted"]();
    }, 0);
    console.log("calling emterpreted_yielder()");
#ifdef BAD_EM_ASM
    Module['_emterpreted_yielder']();
#endif
  });
#ifndef BAD_EM_ASM
  emterpreted_yielder();
#endif
}
    ''')
    run_process([PYTHON, EMCC, 'src.c', '-s', 'EMTERPRETIFY=1', '-s', 'EMTERPRETIFY_ASYNC=1', '-s', 'EMTERPRETIFY_BLACKLIST=["_not_emterpreted"]'])
    self.assertContained('Success', run_js('a.out.js'))

    print('check calling of emterpreted as well')
    run_process([PYTHON, EMCC, 'src.c', '-s', 'EMTERPRETIFY=1', '-s', 'EMTERPRETIFY_ASYNC=1'])
    self.assertContained('Success', run_js('a.out.js'))

    print('check for invalid EM_ASM usage')
    run_process([PYTHON, EMCC, 'src.c', '-s', 'EMTERPRETIFY=1', '-s', 'EMTERPRETIFY_ASYNC=1', '-s', 'EMTERPRETIFY_BLACKLIST=["_not_emterpreted"]', '-DBAD_EM_ASM'])
    self.assertContained('cannot have an EM_ASM on the stack when emterpreter pauses/resumes', run_js('a.out.js', stderr=STDOUT, assert_returncode=None))

  def test_link_with_a_static(self):
    for args in [[], ['-O2']]:
      print(args)
      self.clear()
      open('x.c', 'w').write(r'''
int init_weakref(int a, int b) {
    return a + b;
}
''')
      open('y.c', 'w').write(r'''
static int init_weakref(void) { // inlined in -O2, not in -O0 where it shows up in llvm-nm as 't'
    return 150;
}

int testy(void) {
    return init_weakref();
}
''')
      open('z.c', 'w').write(r'''
extern int init_weakref(int, int);
extern int testy(void);

int main(void) {
    return testy() + init_weakref(5, 6);
}
''')
      Popen([PYTHON, EMCC, 'x.c', '-o', 'x.o']).communicate()
      Popen([PYTHON, EMCC, 'y.c', '-o', 'y.o']).communicate()
      Popen([PYTHON, EMCC, 'z.c', '-o', 'z.o']).communicate()
      Popen([PYTHON, EMAR, 'rc', 'libtest.a', 'y.o']).communicate()
      Popen([PYTHON, EMAR, 'rc', 'libtest.a', 'x.o']).communicate()
      Popen([PYTHON, EMRANLIB, 'libtest.a']).communicate()
      Popen([PYTHON, EMCC, 'z.o', 'libtest.a', '-s', 'NO_EXIT_RUNTIME=0'] + args).communicate()
      out = run_js('a.out.js', assert_returncode=161)

  def test_link_with_bad_o_in_a(self):
    # when building a .a, we force-include all the objects inside it. but, some
    # may not be valid bitcode, e.g. if it contains metadata or something else
    # weird. we should just ignore those
    subprocess.check_call([PYTHON, EMCC, path_from_root('tests', 'hello_world.c'), '-o', 'a.bc'])
    open('bad.bc', 'w').write('this is not a good file, it should be ignored!')
    subprocess.check_call([LLVM_AR, 'r', 'a.a', 'a.bc', 'bad.bc'])
    assert os.path.exists('a.a')
    subprocess.check_call([PYTHON, EMCC, 'a.a'])
    assert os.path.exists('a.out.js'), output
    self.assertContained('hello, world!', run_js('a.out.js'))

  def test_require(self):
    inname = path_from_root('tests', 'hello_world.c')
    Building.emcc(inname, args=['-s', 'ASSERTIONS=0'], output_filename='a.out.js')
    output = run_process(NODE_JS + ['-e', 'require("./a.out.js")'], stdout=PIPE, stderr=PIPE)
    assert output.stdout == 'hello, world!\n' and output.stderr == '', 'expected no output, got\n===\nSTDOUT\n%s\n===\nSTDERR\n%s\n===\n' % (output.stdout, output.stderr)

  def test_require_modularize(self):
    Popen([PYTHON, EMCC, path_from_root('tests', 'hello_world.c'), '-s', 'MODULARIZE=1', '-s', 'ASSERTIONS=0']).communicate()
    src = open('a.out.js').read()
    assert "module.exports = Module;" in src
    output = run_process(NODE_JS + ['-e', 'var m = require("./a.out.js"); m();'], stdout=PIPE, stderr=PIPE)
    assert output.stdout == 'hello, world!\n' and output.stderr == '', 'expected output, got\n===\nSTDOUT\n%s\n===\nSTDERR\n%s\n===\n' % (output.stdout, output.stderr)
    Popen([PYTHON, EMCC, path_from_root('tests', 'hello_world.c'), '-s', 'MODULARIZE=1', '-s', 'EXPORT_NAME="NotModule"', '-s', 'ASSERTIONS=0']).communicate()
    src = open('a.out.js').read()
    assert "module.exports = NotModule;" in src
    output = run_process(NODE_JS + ['-e', 'var m = require("./a.out.js"); m();'], stdout=PIPE, stderr=PIPE)
    assert output.stdout == 'hello, world!\n' and output.stderr == '', 'expected output, got\n===\nSTDOUT\n%s\n===\nSTDERR\n%s\n===\n' % (output.stdout, output.stderr)
    Popen([PYTHON, EMCC, path_from_root('tests', 'hello_world.c'), '-s', 'MODULARIZE=1']).communicate()
    # We call require() twice to ensure it returns wrapper function each time
    output = run_process(NODE_JS + ['-e', 'require("./a.out.js")();var m = require("./a.out.js"); m();'], stdout=PIPE, stderr=PIPE)
    assert output.stdout == 'hello, world!\nhello, world!\n', 'expected output, got\n===\nSTDOUT\n%s\n===\nSTDERR\n%s\n===\n' % (output.stdout, output.stderr)

  def test_define_modularize(self):
    Popen([PYTHON, EMCC, path_from_root('tests', 'hello_world.c'), '-s', 'MODULARIZE=1', '-s', 'ASSERTIONS=0']).communicate()
    with open('a.out.js') as f:
      src = 'var module = 0; ' + f.read()
    with open('a.out.js', 'w') as f:
      f.write(src)
    assert "define([], function() { return Module; });" in src
    output = run_process(NODE_JS + ['-e', 'var m; (global.define = function(deps, factory) { m = factory(); }).amd = true; require("./a.out.js"); m();'], stdout=PIPE, stderr=PIPE)
    assert output.stdout == 'hello, world!\n' and output.stderr == '', 'expected output, got\n===\nSTDOUT\n%s\n===\nSTDERR\n%s\n===\n' % (output.stdout, output.stderr)
    Popen([PYTHON, EMCC, path_from_root('tests', 'hello_world.c'), '-s', 'MODULARIZE=1', '-s', 'EXPORT_NAME="NotModule"', '-s', 'ASSERTIONS=0']).communicate()
    with open('a.out.js') as f:
      src = 'var module = 0; ' + f.read()
    with open('a.out.js', 'w') as f:
      f.write(src)
    assert "define([], function() { return NotModule; });" in src
    output = run_process(NODE_JS + ['-e', 'var m; (global.define = function(deps, factory) { m = factory(); }).amd = true; require("./a.out.js"); m();'], stdout=PIPE, stderr=PIPE)
    assert output.stdout == 'hello, world!\n' and output.stderr == '', 'expected output, got\n===\nSTDOUT\n%s\n===\nSTDERR\n%s\n===\n' % (output.stdout, output.stderr)

  def test_native_optimizer(self):
    def test(args, expected):
      print(args, expected)
      old_debug = os.environ.get('EMCC_DEBUG')
      old_native = os.environ.get('EMCC_NATIVE_OPTIMIZER')
      try:
        os.environ['EMCC_DEBUG'] = '1'
        os.environ['EMCC_NATIVE_OPTIMIZER'] = '1'
        with clean_write_access_to_canonical_temp_dir(self.canonical_temp_dir):
          err = run_process([PYTHON, EMCC, path_from_root('tests', 'hello_world.c'), '-O2', '-s', 'WASM=0'] + args, stderr=PIPE).stderr
      finally:
        if old_debug: os.environ['EMCC_DEBUG'] = old_debug
        else: del os.environ['EMCC_DEBUG']
        if old_native: os.environ['EMCC_NATIVE_OPTIMIZER'] = old_native
        else: del os.environ['EMCC_NATIVE_OPTIMIZER']
      assert err.count('js optimizer using native') == expected, [err, expected]
      assert os.path.exists('a.out.js'), err
      self.assertContained('hello, world!', run_js('a.out.js'))

    test([], 1)
    test(['-s', 'OUTLINING_LIMIT=100000'], 2) # 2, because we run them before and after outline, which is non-native

  def test_emconfigure_js_o(self):
    # issue 2994
    for i in [0, 1, 2]:
      for f in ['hello_world.c', 'files.cpp']:
        print(i, f)
        try:
          os.environ['EMCONFIGURE_JS'] = str(i)
          self.clear()
          Popen([PYTHON, path_from_root('emconfigure'), PYTHON, EMCC, '-c', '-o', 'a.o', path_from_root('tests', f)]).communicate()
          Popen([PYTHON, EMCC, 'a.o']).communicate()
          if f == 'hello_world.c':
            if i == 0:
              assert not os.path.exists('a.out.js') # native .o, not bitcode!
            else:
              assert 'hello, world!' in run_js(self.in_dir('a.out.js'))
          else:
            # file access, need 2 to force js
            if i == 0 or i == 1:
              assert not os.path.exists('a.out.js') # native .o, not bitcode!
            else:
              assert os.path.exists('a.out.js')
        finally:
          del os.environ['EMCONFIGURE_JS']

  @unittest.skipIf('EMCC_DEBUG' in os.environ, 'cannot run in debug mode')
  def test_emcc_c_multi(self):
    with clean_write_access_to_canonical_temp_dir(self.canonical_temp_dir):
      def test(args, llvm_opts=None):
        print(args)
        lib = r'''
          int mult() { return 1; }
        '''

        lib_name = 'libA.c'
        open(lib_name, 'w').write(lib)
        main = r'''
          #include <stdio.h>
          int mult();
          int main() {
            printf("result: %d\n", mult());
            return 0;
          }
        '''
        main_name = 'main.c'
        open(main_name, 'w').write(main)

        try:
          os.environ['EMCC_DEBUG'] = '1'
          err = run_process([PYTHON, EMCC, '-c', main_name, lib_name] + args, stderr=PIPE).stderr
        finally:
          del os.environ['EMCC_DEBUG']

        VECTORIZE = '-disable-loop-vectorization'

        if args:
          assert err.count(VECTORIZE) == 2, err # specified twice, once per file
          assert err.count('emcc: LLVM opts: ' + llvm_opts) == 2, err # corresponding to exactly once per invocation of optimizer
        else:
          assert err.count(VECTORIZE) == 0, err # no optimizations

        Popen([PYTHON, EMCC, main_name.replace('.c', '.o'), lib_name.replace('.c', '.o')]).communicate()

        self.assertContained('result: 1', run_js(os.path.join(self.get_dir(), 'a.out.js')))

      test([])
      test(['-O2'], '-O3')
      test(['-Oz'], '-Oz')
      test(['-Os'], '-Os')

  def test_export_all_3142(self):
    open('src.cpp', 'w').write(r'''
typedef unsigned int Bit32u;

struct S_Descriptor {
    Bit32u limit_0_15   :16;
    Bit32u base_0_15    :16;
    Bit32u base_16_23   :8;
};

class Descriptor
{
public:
    Descriptor() { saved.fill[0]=saved.fill[1]=0; }
    union {
        S_Descriptor seg;
        Bit32u fill[2];
    } saved;
};

Descriptor desc;
    ''')
    try_delete('a.out.js')
    Popen([PYTHON, EMCC, 'src.cpp', '-O2', '-s', 'EXPORT_ALL=1']).communicate()
    assert os.path.exists('a.out.js')

  def test_f0(self):
    Popen([PYTHON, EMCC, path_from_root('tests', 'fasta.cpp'), '-O2', '-s', 'PRECISE_F32=1', '-profiling', '-s', 'WASM=0']).communicate()
    src = open('a.out.js').read()
    assert ' = f0;' in src or ' = f0,' in src

  def test_merge_pair(self):
    def test(filename, full):
      print('----', filename, full)
      Popen([PYTHON, EMCC, path_from_root('tests', filename), '-O1', '-profiling', '-o', 'left.js', '-s', 'WASM=0']).communicate()
      src = open('left.js').read()
      open('right.js', 'w').write(src.replace('function _main() {', 'function _main() { out("replaced"); '))

      self.assertContained('hello, world!', run_js('left.js'))
      self.assertContained('hello, world!', run_js('right.js'))
      self.assertNotContained('replaced', run_js('left.js'))
      self.assertContained('replaced', run_js('right.js'))

      n = src.count('function _')

      def has(i):
        Popen([PYTHON, path_from_root('tools', 'merge_pair.py'), 'left.js', 'right.js', str(i), 'out.js']).communicate()
        return 'replaced' in run_js('out.js')

      assert not has(0), 'same as left'
      assert has(n), 'same as right'
      assert has(n+5), 'same as right, big number is still ok'

      if full:
        change = -1
        for i in range(n):
          if has(i):
            change = i
            break
        assert change > 0 and change <= n

    test('hello_world.cpp', True)
    test('hello_libcxx.cpp', False)

  def test_emmake_emconfigure(self):
    def check(what, args, fail=True, expect=''):
      args = [PYTHON, path_from_root(what)] + args
      print(what, args, fail, expect)
      output = run_process(args, stdout=PIPE, stderr=PIPE, check=False)
      assert ('is a helper for' in output.stderr) == fail
      assert ('Typical usage' in output.stderr) == fail
      self.assertContained(expect, output.stdout)
    check('emmake', [])
    check('emconfigure', [])
    check('emmake', ['--version'])
    check('emconfigure', ['--version'])
    check('emmake', ['make'], fail=False)
    check('emconfigure', ['configure'], fail=False)
    check('emconfigure', ['./configure'], fail=False)
    check('emconfigure', ['cmake'], fail=False)

    open('test.py', 'w').write('''
import os
print(os.environ.get('CROSS_COMPILE'))
''')
    check('emconfigure', [PYTHON, 'test.py'], expect=path_from_root('em'))
    check('emmake', [PYTHON, 'test.py'], expect=path_from_root('em'))

    open('test.py', 'w').write('''
import os
print(os.environ.get('NM'))
''')
    check('emconfigure', [PYTHON, 'test.py'], expect=tools.shared.LLVM_NM)

  def test_emmake_python(self):
    # simulates a configure/make script that looks for things like CC, AR, etc., and which we should
    # not confuse by setting those vars to something containing `python X` as the script checks for
    # the existence of an executable.
    result = run_process([PYTHON, path_from_root('emmake.py'), PYTHON, path_from_root('tests', 'emmake', 'make.py')], stdout=PIPE, stderr=PIPE)
    print(result.stdout, result.stderr)

  def test_sdl2_config(self):
    for args, expected in [
      [['--version'], '2.0.0'],
      [['--cflags'], '-s USE_SDL=2'],
      [['--libs'], '-s USE_SDL=2'],
      [['--cflags', '--libs'], '-s USE_SDL=2'],
    ]:
      print(args, expected)
      out = run_process([PYTHON, path_from_root('system', 'bin', 'sdl2-config')] + args, stdout=PIPE, stderr=PIPE).stdout
      assert expected in out, out
      print('via emmake')
      out = run_process([PYTHON, path_from_root('emmake'), 'sdl2-config'] + args, stdout=PIPE, stderr=PIPE).stdout
      assert expected in out, out

  def test_module_onexit(self):
    open('src.cpp', 'w').write(r'''
#include <emscripten.h>
int main() {
  EM_ASM({
    Module['onExit'] = function(status) { out('exiting now, status ' + status) };
  });
  return 14;
}
''')
    try_delete('a.out.js')
    run_process([PYTHON, EMCC, 'src.cpp', '-s', 'NO_EXIT_RUNTIME=0'])
    self.assertContained('exiting now, status 14', run_js('a.out.js', assert_returncode=14))

  def test_underscore_exit(self):
    open('src.cpp', 'w').write(r'''
#include <unistd.h>
int main() {
  _exit(0); // should not end up in an infinite loop with non-underscore exit
}
''')
    subprocess.check_call([PYTHON, EMCC, 'src.cpp'])
    self.assertContained('', run_js('a.out.js', assert_returncode=0))

  def test_file_packager_huge(self):
    MESSAGE = 'warning: file packager is creating an asset bundle of 257 MB. this is very large, and browsers might have trouble loading it'
    open('huge.dat', 'w').write('a'*(1024*1024*257))
    open('tiny.dat', 'w').write('a')
    err = run_process([PYTHON, FILE_PACKAGER, 'test.data', '--preload', 'tiny.dat'], stdout=PIPE, stderr=PIPE).stderr
    self.assertNotContained(MESSAGE, err)
    err = run_process([PYTHON, FILE_PACKAGER, 'test.data', '--preload', 'huge.dat'], stdout=PIPE, stderr=PIPE).stderr
    self.assertContained(MESSAGE, err)
    self.clear()

  def test_massive_alloc(self):
    if SPIDERMONKEY_ENGINE not in JS_ENGINES: self.skipTest('cannot run without spidermonkey, node cannnot alloc huge arrays')

    open(os.path.join(self.get_dir(), 'main.cpp'), 'w').write(r'''
#include <stdio.h>
#include <stdlib.h>

int main() {
  volatile int x = (int)malloc(1024*1024*1400);
  return x == 0; // can't alloc it, but don't fail catastrophically, expect null
}
    ''')
    run_process([PYTHON, EMCC, os.path.join(self.get_dir(), 'main.cpp'), '-s', 'ALLOW_MEMORY_GROWTH=1', '-s', 'WASM=0'])
    # just care about message regarding allocating over 1GB of memory
    output = run_js('a.out.js', stderr=PIPE, full_output=True, engine=SPIDERMONKEY_ENGINE)
    self.assertContained('''Warning: Enlarging memory arrays, this is not fast! 16777216,1543503872\n''', output)
    print('wasm')
    run_process([PYTHON, EMCC, os.path.join(self.get_dir(), 'main.cpp'), '-s', 'ALLOW_MEMORY_GROWTH=1'])
    # no message about growth, just check return code
    run_js('a.out.js', stderr=PIPE, full_output=True, engine=SPIDERMONKEY_ENGINE)

  def test_failing_alloc(self):
    for pre_fail, post_fail, opts in [
      ('', '', []),
      ('EM_ASM( Module.temp = HEAP32[DYNAMICTOP_PTR>>2] );', 'EM_ASM( assert(Module.temp === HEAP32[DYNAMICTOP_PTR>>2], "must not adjust DYNAMICTOP when an alloc fails!") );', []),
      ('', '', ['-s', 'SPLIT_MEMORY=' + str(16*1024*1024), '-DSPLIT', '-s', 'WASM=0']),
      # also test non-wasm in normal mode
      ('', '', ['-s', 'WASM=0']),
      ('EM_ASM( Module.temp = HEAP32[DYNAMICTOP_PTR>>2] );', 'EM_ASM( assert(Module.temp === HEAP32[DYNAMICTOP_PTR>>2], "must not adjust DYNAMICTOP when an alloc fails!") );', ['-s', 'WASM=0']),
    ]:
      for growth in [0, 1]:
        for aborting in [0, 1]:
          open(os.path.join(self.get_dir(), 'main.cpp'), 'w').write(r'''
#include <stdio.h>
#include <stdlib.h>
#include <vector>
#include <assert.h>
#include <emscripten.h>

#define CHUNK_SIZE (10*1024*1024)

int main() {
  EM_ASM({
    // we want to allocate a lot until eventually we can't anymore. to simulate that, we limit how much
    // can be allocated by Buffer, so that if we don't hit a limit before that, we don't keep going into
    // swap space and other bad things.
    var old = Module['reallocBuffer'];
    Module['reallocBuffer'] = function(size) {
      if (size > 500 * 1024 * 1024) {
        return null;
      }
      return old(size);
    };
  });

  std::vector<void*> allocs;
  bool has = false;
  while (1) {
    printf("trying an allocation\n");
    %s
    void* curr = malloc(CHUNK_SIZE);
    if (!curr) {
      %s
      break;
    }
    has = true;
    printf("allocated another chunk, %%d so far\n", allocs.size());
    allocs.push_back(curr);
  }
  assert(has);
  printf("an allocation failed!\n");
#ifdef SPLIT
  return 0;
#endif
  while (1) {
    assert(allocs.size() > 0);
    void *curr = allocs.back();
    allocs.pop_back();
    free(curr);
    printf("freed one\n");
    if (malloc(CHUNK_SIZE)) break;
  }
  printf("managed another malloc!\n");
}
''' % (pre_fail, post_fail))
          args = [PYTHON, EMCC, os.path.join(self.get_dir(), 'main.cpp')] + opts
          if growth: args += ['-s', 'ALLOW_MEMORY_GROWTH=1']
          if not aborting: args += ['-s', 'ABORTING_MALLOC=0']
          print('test_failing_alloc', args, pre_fail)
          check_execute(args)
          # growth also disables aborting
          can_manage_another = (not aborting) or growth
          split = '-DSPLIT' in args
          print('can manage another:', can_manage_another, 'split:', split)
          output = run_js('a.out.js', stderr=PIPE, full_output=True, assert_returncode=0 if can_manage_another else None)
          if can_manage_another:
            self.assertContained('''an allocation failed!\n''', output)
            if not split:
              # split memory allocation may fail due to GC objects no longer being allocatable,
              # and we can't expect to recover from that deterministically. So just check we
              # get to the fail.
              # otherwise, we should fail eventually, then free, then succeed
              self.assertContained('''managed another malloc!\n''', output)
          else:
            # we should see an abort
            self.assertContained('''abort("Cannot enlarge memory arrays''', output)
            self.assertContained(('''higher than the current value 16777216,''', '''higher than the current value 33554432,'''), output)
            self.assertContained('''compile with  -s ALLOW_MEMORY_GROWTH=1 ''', output)
            self.assertContained('''compile with  -s ABORTING_MALLOC=0 ''', output)

  def test_libcxx_minimal(self):
    open('vector.cpp', 'w').write(r'''
#include <vector>
int main(int argc, char** argv) {
  std::vector<void*> v;
  for (int i = 0 ; i < argc; i++) {
    v.push_back(nullptr);
  }
  return v.size();
}
''')

    Popen([PYTHON, EMCC, '-O2', 'vector.cpp', '-o', 'vector.js']).communicate()[1]
    Popen([PYTHON, EMCC, '-O2', path_from_root('tests', 'hello_libcxx.cpp'), '-o', 'iostream.js']).communicate()[1]

    vector = os.stat('vector.js').st_size
    iostream = os.stat('iostream.js').st_size
    print(vector, iostream)

    assert vector > 1000
    assert 2.5*vector < iostream # we can strip out almost all of libcxx when just using vector

  def test_emulated_function_pointers(self):
    src = r'''
      #include <emscripten.h>
      typedef void (*fp)();
      int main(int argc, char **argv) {
        volatile fp f = 0;
        EM_ASM({
          if (typeof FUNCTION_TABLE_v !== 'undefined') {
            out('function table: ' + FUNCTION_TABLE_v);
          } else {
            out('no visible function tables');
          }
        });
        if (f) f();
        return 0;
      }
    '''
    open('src.c', 'w').write(src)
    def test(args, expected):
      print(args, expected)
      out, err = Popen([PYTHON, EMCC, 'src.c', '-s', 'WASM=0'] + args, stderr=PIPE).communicate()
      self.assertContained(expected, run_js(self.in_dir('a.out.js')))

    for opts in [0, 1, 2, 3]:
      test(['-O' + str(opts)], 'no visible function tables')
      test(['-O' + str(opts), '-s', 'EMULATED_FUNCTION_POINTERS=1'], 'function table: ')

  def test_emulated_function_pointers_2(self):
    src = r'''
      #include <emscripten.h>
      typedef void (*fp)();
      void one() { EM_ASM( out('one') ); }
      void two() { EM_ASM( out('two') ); }
      void test() {
        volatile fp f = one;
        f();
        f = two;
        f();
      }
      int main(int argc, char **argv) {
        test();
        // swap them!
        EM_ASM_INT({
          var one = $0;
          var two = $1;
          if (typeof FUNCTION_TABLE_v === 'undefined') {
            out('no');
            return;
          }
          var temp = FUNCTION_TABLE_v[one];
          FUNCTION_TABLE_v[one] = FUNCTION_TABLE_v[two];
          FUNCTION_TABLE_v[two] = temp;
        }, (int)&one, (int)&two);
        test();
        return 0;
      }
    '''
    open('src.c', 'w').write(src)

    flipped = 'one\ntwo\ntwo\none\n'
    unchanged = 'one\ntwo\none\ntwo\n'
    no_table = 'one\ntwo\nno\none\ntwo\n'

    def test(args, expected):
      print(args, expected.replace('\n', ' '))
      Popen([PYTHON, EMCC, 'src.c', '-s', 'WASM=0'] + args).communicate()
      self.assertContained(expected, run_js(self.in_dir('a.out.js')))

    for opts in [0, 1, 2]:
      test(['-O' + str(opts)], no_table)
      test(['-O' + str(opts), '-s', 'EMULATED_FUNCTION_POINTERS=1'], flipped)
      test(['-O' + str(opts), '-s', 'EMULATED_FUNCTION_POINTERS=2'], flipped)
      test(['-O' + str(opts), '-s', 'EMULATED_FUNCTION_POINTERS=1', '-s', 'RELOCATABLE=1'], flipped)
      test(['-O' + str(opts), '-s', 'EMULATED_FUNCTION_POINTERS=2', '-s', 'RELOCATABLE=1'], unchanged) # with both of those, we optimize and you cannot flip them
      test(['-O' + str(opts), '-s', 'MAIN_MODULE=1'], unchanged) # default for modules is optimized
      test(['-O' + str(opts), '-s', 'MAIN_MODULE=1', '-s', 'EMULATED_FUNCTION_POINTERS=2'], unchanged)
      test(['-O' + str(opts), '-s', 'MAIN_MODULE=1', '-s', 'EMULATED_FUNCTION_POINTERS=1'], flipped) # but you can disable that

  def test_minimal_dynamic(self):
    for wasm in (1, 0):
      print('wasm?', wasm)
      library_file = 'library.wasm' if wasm else 'library.js'
      def test(main_args=[], library_args=[], expected='hello from main\nhello from library'):
        print('testing', main_args, library_args)
        self.clear()
        open('library.c', 'w').write(r'''
          #include <stdio.h>
          void library_func() {
          #ifdef USE_PRINTF
            printf("hello from library: %p\n", (int)&library_func);
          #else
            puts("hello from library");
          #endif
          }
        ''')
        check_execute([PYTHON, EMCC, 'library.c', '-s', 'SIDE_MODULE=1', '-O2', '-o', library_file, '-s', 'WASM=' + str(wasm)] + library_args)
        open('main.c', 'w').write(r'''
          #include <dlfcn.h>
          #include <stdio.h>
          int main() {
            puts("hello from main");
            void *lib_handle = dlopen("%s", 0);
            if (!lib_handle) {
              puts("cannot load side module");
              return 1;
            }
            typedef void (*voidfunc)();
            voidfunc x = (voidfunc)dlsym(lib_handle, "library_func");
            if (!x) puts("cannot find side function");
            else x();
          }
        ''' % library_file)
        check_execute([PYTHON, EMCC, 'main.c', '-s', 'MAIN_MODULE=1', '--embed-file', library_file, '-O2', '-s', 'WASM=' + str(wasm)] + main_args)
        self.assertContained(expected, run_js('a.out.js', assert_returncode=None, stderr=subprocess.STDOUT))
        size = os.stat('a.out.js').st_size
        if wasm:
          size += os.stat('a.out.wasm').st_size
        side_size = os.stat(library_file).st_size
        print('  sizes:', size, side_size)
        return (size, side_size)

      def percent_diff(x, y):
        small = min(x, y)
        large = max(x, y)
        return float(100*large)/small - 100

      # main module tests

      full     = test()
      printf   = test(                                   library_args=['-DUSE_PRINTF'])                       # printf is not used in main, but libc was linked in, so it's there
      dce      = test(main_args=['-s', 'MAIN_MODULE=2'])                                                      # dce in main, and side happens to be ok since it uses puts as well
      dce_fail = test(main_args=['-s', 'MAIN_MODULE=2'], library_args=['-DUSE_PRINTF'], expected=('cannot', 'undefined')) # printf is not used in main, and we dce, so we failz
      dce_save = test(main_args=['-s', 'MAIN_MODULE=2', '-s', 'EXPORTED_FUNCTIONS=["_main", "_printf"]'],
                                                         library_args=['-DUSE_PRINTF'])                       # exporting printf in main keeps it alive for the library

      assert percent_diff(full[0], printf[0]) < 4
      assert percent_diff(dce[0], dce_fail[0]) < 4
      assert dce[0] < 0.2*full[0] # big effect, 80%+ is gone
      assert dce_save[0] > 1.1*dce[0] # save exported all of printf

      # side module tests

      side_dce_fail = test(library_args=['-s', 'SIDE_MODULE=2'], expected='cannot find side function') # mode 2, so dce in side, but library_func is not exported, so it is dce'd
      side_dce_work = test(library_args=['-s', 'SIDE_MODULE=2', '-s', 'EXPORTED_FUNCTIONS=["_library_func"]'], expected='hello from library') # mode 2, so dce in side, and library_func is not exported

      assert side_dce_fail[1] < 0.95*side_dce_work[1] # removing that function saves a chunk

  def test_ld_library_path(self):
    open('hello1.c', 'w').write(r'''
#include <stdio.h>

void
hello1 ()
{
  printf ("Hello1\n");
  return;
}

''')
    open('hello2.c', 'w').write(r'''
#include <stdio.h>

void
hello2 ()
{
  printf ("Hello2\n");
  return;
}

''')
    open('hello3.c', 'w').write(r'''
#include <stdio.h>

void
hello3 ()
{
  printf ("Hello3\n");
  return;
}

''')
    open('hello4.c', 'w').write(r'''
#include <stdio.h>
#include <math.h>

double
hello4 (double x)
{
  printf ("Hello4\n");
  return fmod(x, 2.0);
}

''')
    open('pre.js', 'w').write(r'''
Module['preRun'].push(function (){
  ENV['LD_LIBRARY_PATH']='/lib:/usr/lib';
});
''')
    open('main.c', 'w').write(r'''
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <dlfcn.h>

int
main()
{
  void *h;
  void (*f) ();
  double (*f2) (double);

  h = dlopen ("libhello1.wasm", RTLD_NOW);
  f = dlsym (h, "hello1");
  f();
  dlclose (h);
  h = dlopen ("libhello2.wasm", RTLD_NOW);
  f = dlsym (h, "hello2");
  f();
  dlclose (h);
  h = dlopen ("libhello3.wasm", RTLD_NOW);
  f = dlsym (h, "hello3");
  f();
  dlclose (h);
  h = dlopen ("/usr/local/lib/libhello4.wasm", RTLD_NOW);
  f2 = dlsym (h, "hello4");
  double result = f2(5.5);
  dlclose (h);

  if (result == 1.5) {
    printf("Ok\n");
  }
  return 0;
}

''')

    Popen([PYTHON, EMCC, '-o', 'libhello1.wasm', 'hello1.c', '-s', 'SIDE_MODULE=1']).communicate()
    Popen([PYTHON, EMCC, '-o', 'libhello2.wasm', 'hello2.c', '-s', 'SIDE_MODULE=1']).communicate()
    Popen([PYTHON, EMCC, '-o', 'libhello3.wasm', 'hello3.c', '-s', 'SIDE_MODULE=1']).communicate()
    Popen([PYTHON, EMCC, '-o', 'libhello4.wasm', 'hello4.c', '-s', 'SIDE_MODULE=1']).communicate()
    Popen([PYTHON, EMCC, '-o', 'main.js', 'main.c', '-s', 'MAIN_MODULE=1', '-s', 'TOTAL_MEMORY=' + str(32*1024*1024),
           '--embed-file', 'libhello1.wasm@/lib/libhello1.wasm',
           '--embed-file', 'libhello2.wasm@/usr/lib/libhello2.wasm',
           '--embed-file', 'libhello3.wasm@/libhello3.wasm',
           '--embed-file', 'libhello4.wasm@/usr/local/lib/libhello4.wasm',
           '--pre-js', 'pre.js']).communicate()
    out = run_js('main.js')
    self.assertContained('Hello1', out)
    self.assertContained('Hello2', out)
    self.assertContained('Hello3', out)
    self.assertContained('Hello4', out)
    self.assertContained('Ok', out)

  def test_dlopen_rtld_global(self):
    # TODO: wasm support. this test checks RTLD_GLOBAL where a module is loaded
    #       before the module providing a global it needs is. in asm.js we use JS
    #       to create a redirection function. In wasm we just have wasm, so we
    #       need to introspect the wasm module. Browsers may add that eventually,
    #       or we could ship a little library that does it.
    open('hello1.c', 'w').write(r'''
#include <stdio.h>

extern int hello1_val;
int hello1_val=3;

void
hello1 (int i)
{
  printf ("hello1_val by hello1:%d\n",hello1_val);
  printf ("Hello%d\n",i);
}
''')
    open('hello2.c', 'w').write(r'''
#include <stdio.h>

extern int hello1_val;
extern void hello1 (int);

void
hello2 (int i)
{
  void (*f) (int);
  printf ("hello1_val by hello2:%d\n",hello1_val);
  f = hello1;
  f(i);
}
''')
    open('main.c', 'w').write(r'''
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <dlfcn.h>

int
main(int argc,char** argv)
{
  void *h;
  void *h2;
  void (*f) (int);
  h = dlopen ("libhello1.js", RTLD_NOW|RTLD_GLOBAL);
  h2 = dlopen ("libhello2.js", RTLD_NOW|RTLD_GLOBAL);
  f = dlsym (h, "hello1");
  f(1);
  f = dlsym (h2, "hello2");
  f(2);
  dlclose (h);
  dlclose (h2);
  return 0;
}
''')

    Popen([PYTHON, EMCC, '-o', 'libhello1.js', 'hello1.c', '-s', 'SIDE_MODULE=1', '-s', 'WASM=0']).communicate()
    Popen([PYTHON, EMCC, '-o', 'libhello2.js', 'hello2.c', '-s', 'SIDE_MODULE=1', '-s', 'WASM=0']).communicate()
    Popen([PYTHON, EMCC, '-o', 'main.js', 'main.c', '-s', 'MAIN_MODULE=1', '-s', 'WASM=0',
           '--embed-file', 'libhello1.js',
           '--embed-file', 'libhello2.js']).communicate()
    out = run_js('main.js')
    self.assertContained('Hello1', out)
    self.assertContained('Hello2', out)
    self.assertContained('hello1_val by hello1:3', out)
    self.assertContained('hello1_val by hello2:3', out)

  def test_debug_asmLastOpts(self):
    open('src.c', 'w').write(r'''
#include <stdio.h>
struct Dtlink_t
{   struct Dtlink_t*   right;  /* right child      */
        union
        { unsigned int  _hash;  /* hash value       */
          struct Dtlink_t* _left;  /* left child       */
        } hl;
};
int treecount(register struct Dtlink_t* e)
{
  return e ? treecount(e->hl._left) + treecount(e->right) + 1 : 0;
}
int main() {
  printf("hello, world!\n");
}
''')
    out, err = Popen([PYTHON, EMCC, 'src.c', '-s', 'EXPORTED_FUNCTIONS=["_main", "_treecount"]', '--minify', '0', '-g4', '-Oz']).communicate()
    self.assertContained('hello, world!', run_js('a.out.js'))

  def test_meminit_crc(self):
    with open('src.c', 'w') as f:
      f.write(r'''
#include <stdio.h>
int main() { printf("Mary had a little lamb.\n"); }
''')
    out, err = Popen([PYTHON, EMCC, 'src.c', '-O2', '--memory-init-file', '0', '-s', 'MEM_INIT_METHOD=2', '-s', 'ASSERTIONS=1', '-s', 'WASM=0']).communicate()
    with open('a.out.js', 'r') as f:
      d = f.read()
    d = d.replace('Mary had', 'Paul had')
    with open('a.out.js', 'w') as f:
      f.write(d)
    out = run_js('a.out.js', assert_returncode=None, stderr=subprocess.STDOUT)
    self.assertContained('Assertion failed: memory initializer checksum', out)

  def test_emscripten_print_double(self):
    with open('src.c', 'w') as f:
      f.write(r'''
#include <stdio.h>
#include <assert.h>
#include <emscripten.h>

void test(double d) {
  char buffer[100], buffer2[100];
  unsigned len, len2, len3;
  len = emscripten_print_double(d, NULL, -1);
  len2 = emscripten_print_double(d, buffer, len+1);
  assert(len == len2);
  buffer[len] = 0;
  len3 = snprintf(buffer2, 100, "%g", d);
  printf("|%g : %u : %s : %s : %d|\n", d, len, buffer, buffer2, len3);
}
int main() {
  printf("\n");
  test(0);
  test(1);
  test(-1);
  test(1.234);
  test(-1.234);
  test(1.1234E20);
  test(-1.1234E20);
  test(1.1234E-20);
  test(-1.1234E-20);
  test(1.0/0.0);
  test(-1.0/0.0);
}
''')
    Popen([PYTHON, EMCC, 'src.c']).communicate()
    out = run_js('a.out.js')
    self.assertContained('''
|0 : 1 : 0 : 0 : 1|
|1 : 1 : 1 : 1 : 1|
|-1 : 2 : -1 : -1 : 2|
|1.234 : 5 : 1.234 : 1.234 : 5|
|-1.234 : 6 : -1.234 : -1.234 : 6|
|1.1234e+20 : 21 : 112340000000000000000 : 1.1234e+20 : 10|
|-1.1234e+20 : 22 : -112340000000000000000 : -1.1234e+20 : 11|
|1.1234e-20 : 10 : 1.1234e-20 : 1.1234e-20 : 10|
|-1.1234e-20 : 11 : -1.1234e-20 : -1.1234e-20 : 11|
|inf : 8 : Infinity : inf : 3|
|-inf : 9 : -Infinity : -inf : 4|
''', out)

  def test_no_warn_exported_jslibfunc(self):
    err = run_process([PYTHON, EMCC, path_from_root('tests', 'hello_world.c'), '-s', 'DEFAULT_LIBRARY_FUNCS_TO_INCLUDE=["alGetError"]', '-s', 'EXPORTED_FUNCTIONS=["_main", "_alGetError"]'], stdout=PIPE, stderr=PIPE).stderr
    self.assertNotContained('''function requested to be exported, but not implemented: "_alGetError"''', err)

  @no_wasm_backend()
  def test_almost_asm_warning(self):
    warning = "[-Walmost-asm]"
    for args, expected in [(['-O1', '-s', 'SPLIT_MEMORY=8388608', '-s', 'TOTAL_MEMORY=' + str(16*1024*1024)], True),  # default
                           # suppress almost-asm warning when building with ALLOW_MEMORY_GROWTH
                           (['-O1', '-s', 'ALLOW_MEMORY_GROWTH=1', '-Wno-almost-asm'], False),
                           # suppress almost-asm warning when building with SPLIT_MEMORY
                           (['-O1', '-s', 'SPLIT_MEMORY=8388608', '-s', 'TOTAL_MEMORY=' + str(16*1024*1024), '-Wno-almost-asm'], False),
                           # last warning flag should "win"
                           (['-O1', '-s', 'ALLOW_MEMORY_GROWTH=1', '-Wno-almost-asm', '-Walmost-asm'], True)]:
      print(args, expected)
      err = run_process([PYTHON, EMCC, path_from_root('tests', 'hello_world.c'), '-s', 'WASM=0'] + args, stderr=PIPE).stderr
      assert (warning in err) == expected, err
      if not expected:
        assert err == '', err

  def test_static_syscalls(self):
    Popen([PYTHON, EMCC, path_from_root('tests', 'hello_world.c')]).communicate()
    src = open('a.out.js').read()
    matches = re.findall('''function ___syscall(\d+)\(''', src)
    print('seen syscalls:', matches)
    assert set(matches) == set(['6', '54', '140', '146']) # close, ioctl, llseek, writev

  def test_emcc_dev_null(self):
    if WINDOWS: self.skipTest('posix-only')
    out = run_process([PYTHON, EMCC, '-dM', '-E', '-x', 'c', '/dev/null'], stdout=PIPE).stdout
    self.assertContained('#define __EMSCRIPTEN__ 1', out) # all our defines should show up

  def test_umask_0(self):
    open('src.c', 'w').write(r'''
#include <sys/stat.h>
#include <stdio.h>
int main() {
  umask(0);
  printf("hello, world!\n");
}''')
    Popen([PYTHON, EMCC, 'src.c']).communicate()
    self.assertContained('hello, world!', run_js('a.out.js'))

  def test_no_missing_symbols(self): # simple hello world should not show any missing symbols
    check_execute([PYTHON, EMCC, path_from_root('tests', 'hello_world.c'), '-s', 'ERROR_ON_UNDEFINED_SYMBOLS=1'])

    # main() is implemented in C, and even if requested from JS, we should not warn
    open('library_foo.js', 'w').write('''
mergeInto(LibraryManager.library, {
  my_js__deps: ['main'],
  my_js: (function() {
      return function() {
        console.log("hello " + _nonexistingvariable);
      };
  }()),
});
''')
    open('test.cpp', 'w').write('''
#include <stdio.h>
#include <stdlib.h>

extern "C" {
  extern void my_js();
}

int main() {
  my_js();
  return EXIT_SUCCESS;
}
''')
    check_execute([PYTHON, EMCC, 'test.cpp', '-s', 'ERROR_ON_UNDEFINED_SYMBOLS=1', '--js-library', 'library_foo.js'])

    # but we do error on a missing js var
    open('library_foo.js', 'w').write('''
mergeInto(LibraryManager.library, {
  my_js__deps: ['main', 'nonexistingvariable'],
  my_js: (function() {
      return function() {
        console.log("hello " + _nonexistingvariable);
      };
  }()),
});
''')
    err = run_process([PYTHON, EMCC, 'test.cpp', '-s', 'ERROR_ON_UNDEFINED_SYMBOLS=1', '--js-library', 'library_foo.js'], stderr=PIPE, check=False).stderr
    assert 'unresolved symbol' in err

    # and also for missing C code, of course (without the --js-library, it's just a missing C method)
    err = run_process([PYTHON, EMCC, 'test.cpp', '-s', 'ERROR_ON_UNDEFINED_SYMBOLS=1'], stderr=PIPE, check=False).stderr
    assert 'unresolved symbol' in err

  def test_realpath(self):
    open('src.c', 'w').write(r'''
#include <stdlib.h>
#include <stdio.h>
#include <errno.h>

#define TEST_PATH "/boot/README.txt"

int
main(int argc, char **argv)
{
  errno = 0;
  char *t_realpath_buf = realpath(TEST_PATH, NULL);
  if (NULL == t_realpath_buf) {
    perror("Resolve failed");
    return 1;
  } else {
    printf("Resolved: %s\n", t_realpath_buf);
    free(t_realpath_buf);
    return 0;
  }
}
''')
    if not os.path.exists('boot'):
      os.mkdir('boot')
    open(os.path.join('boot', 'README.txt'), 'w').write(' ')
    Popen([PYTHON, EMCC, 'src.c', '--embed-file', 'boot']).communicate()
    self.assertContained('Resolved: /boot/README.txt', run_js('a.out.js'))

  def test_realpath_nodefs(self):
    open('src.c', 'w').write(r'''
#include <stdlib.h>
#include <stdio.h>
#include <errno.h>
#include <emscripten.h>

#define TEST_PATH "/working/TEST_NODEFS.txt"

int
main(int argc, char **argv)
{
  errno = 0;
  EM_ASM({
    FS.mkdir('/working');
    FS.mount(NODEFS, { root: '.' }, '/working');
  });
  char *t_realpath_buf = realpath(TEST_PATH, NULL);
  if (NULL == t_realpath_buf) {
    perror("Resolve failed");
    return 1;
  } else {
    printf("Resolved: %s\n", t_realpath_buf);
    free(t_realpath_buf);
    return 0;
  }
}
''')
    open('TEST_NODEFS.txt', 'w').write(' ')
    Popen([PYTHON, EMCC, 'src.c']).communicate()
    self.assertContained('Resolved: /working/TEST_NODEFS.txt', run_js('a.out.js'))

  def test_realpath_2(self):
    open('src.c', 'w').write(r'''
#include <stdlib.h>
#include <stdio.h>
#include <errno.h>

int testrealpath(const char* path)    {
  errno = 0;
  char *t_realpath_buf = realpath(path, NULL);
  if (NULL == t_realpath_buf) {
    printf("Resolve failed: \"%s\"\n",path);fflush(stdout);
    return 1;
  } else {
    printf("Resolved: \"%s\" => \"%s\"\n", path, t_realpath_buf);fflush(stdout);
    free(t_realpath_buf);
    return 0;
  }
}

int main(int argc, char **argv)
{
    // files:
    testrealpath("testfile.txt");
    testrealpath("Folder/testfile.txt");
    testrealpath("testnonexistentfile.txt");
    // folders
    testrealpath("Folder");
    testrealpath("/Folder");
    testrealpath("./");
    testrealpath("");
    testrealpath("/");
    return 0;
}
''')
    open('testfile.txt', 'w').write('')
    if not os.path.exists('Folder'): os.mkdir('Folder')
    open(os.path.join('Folder', 'testfile.txt'), 'w').write('')
    check_execute([PYTHON, EMCC, 'src.c', '--embed-file', 'testfile.txt', '--embed-file', 'Folder'])
    self.assertContained('''Resolved: "testfile.txt" => "/testfile.txt"
Resolved: "Folder/testfile.txt" => "/Folder/testfile.txt"
Resolve failed: "testnonexistentfile.txt"
Resolved: "Folder" => "/Folder"
Resolved: "/Folder" => "/Folder"
Resolved: "./" => "/"
Resolve failed: ""
Resolved: "/" => "/"
''', run_js('a.out.js'))

  def test_no_warnings(self):
    # build once before to make sure system libs etc. exist
    subprocess.check_call([PYTHON, EMCC, path_from_root('tests', 'hello_libcxx.cpp')])
    # check that there is nothing in stderr for a regular compile
    err = run_process([PYTHON, EMCC, path_from_root('tests', 'hello_libcxx.cpp')], stderr=PIPE).stderr
    assert err == '', err

  def test_emterpreter_file_suggestion(self):
    for linkable in [0, 1]:
      for to_file in [0, 1]:
        self.clear()
        cmd = [PYTHON, EMCC, '-s', 'EMTERPRETIFY=1', path_from_root('tests', 'hello_libcxx.cpp'), '-s', 'LINKABLE=' + str(linkable), '-O1', '-s', 'USE_ZLIB=1']
        if to_file:
          cmd += ['-s', 'EMTERPRETIFY_FILE="code.dat"']
        print(cmd)
        stderr = run_process(cmd, stderr=PIPE).stderr
        need_warning = linkable and not to_file
        assert ('''warning: emterpreter bytecode is fairly large''' in stderr) == need_warning, stderr
        assert ('''It is recommended to use  -s EMTERPRETIFY_FILE=..''' in stderr) == need_warning, stderr

  def test_llvm_lto(self):
    sizes = {}
    lto_levels = [0, 1, 2, 3]
    for lto in lto_levels:
      cmd = [PYTHON, EMCC, path_from_root('tests', 'hello_libcxx.cpp'), '-O2', '--llvm-lto', str(lto)]
      print(cmd)
      check_execute(cmd)
      self.assertContained('hello, world!', run_js('a.out.js'))
      sizes[lto] = os.stat('a.out.wasm').st_size
    print(sizes)

    # LTO sizes should be distinct
    for i in lto_levels:
      assert sizes[i] not in set(sizes).difference(set([sizes[i]]))

    # LTO should reduce code size
    # Skip mode 2 because it has historically increased code size, but not always
    assert sizes[1] < sizes[0]
    assert sizes[3] < sizes[0]

  def test_dlmalloc_modes(self):
    open('src.cpp', 'w').write(r'''
      #include <stdlib.h>
      #include <stdio.h>
      int main() {
        void* c = malloc(1024);
        free(c);
        free(c);
        printf("double-freed\n");
      }
    ''')
    subprocess.check_call([PYTHON, EMCC, 'src.cpp'])
    self.assertContained('double-freed', run_js('a.out.js'))
    # in debug mode, the double-free is caught
    subprocess.check_call([PYTHON, EMCC, 'src.cpp', '-g'])
    seen_error = False
    out = '?'
    try:
      out = run_js('a.out.js')
    except:
      seen_error = True
    assert seen_error, out

  def test_mallocs(self):
    for opts in [[], ['-O2']]:
      print(opts)
      sizes = {}
      for malloc, name in (
        ('dlmalloc', 'dlmalloc'),
        (None, 'default'),
        ('emmalloc', 'emmalloc')
      ):
        print(malloc, name)
        cmd = [PYTHON, EMCC, path_from_root('tests', 'hello_libcxx.cpp'), '-s', 'WASM=1', '-o', 'a.out.js'] + opts
        if malloc:
          cmd += ['-s', 'MALLOC="%s"' % malloc]
        print(cmd)
        run_process(cmd)
        sizes[name] = os.stat('a.out.wasm').st_size
      print(sizes)
      assert sizes['dlmalloc'] == sizes['default'], 'dlmalloc is the default'
      assert sizes['emmalloc']  < sizes['dlmalloc'] - 5000, 'emmalloc is much smaller'

  def test_split_memory(self): # make sure multiple split memory chunks get used
    open('src.c', 'w').write(r'''
#include <emscripten.h>
#include <stdlib.h>
int main() {
  int x = 5;
  EM_ASM({
    var allocs = [];
    allocs.push([]);
    allocs.push([]);
    allocs.push([]);
    allocs.push([]);
    var ptr = $0;
    assert(ptr >= STACK_BASE && ptr < STACK_MAX, 'ptr should be on stack, but ' + [STACK_BASE, STACK_MAX, ptr]);
    function getIndex(x) {
      return x >> SPLIT_MEMORY_BITS;
    }
    assert(ptr < SPLIT_MEMORY, 'in first chunk');
    assert(getIndex(ptr) === 0, 'definitely in first chunk');
    // allocate into other chunks
    do {
      var t = Module._malloc(1024*1024);
      allocs[getIndex(t)].push(t);
      out('allocating, got in ' + getIndex(t));
    } while (getIndex(t) === 0);
    assert(getIndex(t) === 1, 'allocated into second chunk');
    do {
      var t = Module._malloc(1024*1024);
      allocs[getIndex(t)].push(t);
      out('more allocating, got in ' + getIndex(t));
    } while (getIndex(t) === 1);
    assert(getIndex(t) === 2, 'into third chunk');
    do {
      var t = Module._malloc(1024*1024);
      allocs[getIndex(t)].push(t);
      out('more allocating, got in ' + getIndex(t));
    } while (getIndex(t) === 2);
    assert(getIndex(t) === 3, 'into third chunk');
    // write values
    assert(allocs[1].length > 5 && allocs[2].length > 5);
    for (var i = 0; i < allocs[1].length; i++) {
      HEAPU8[allocs[1][i]] = i & 255
    }
    for (var i = 0; i < allocs[2].length; i++) {
      HEAPU8[allocs[2][i]] = (i*i) & 255;
    }
    for (var i = 0; i < allocs[1].length; i++) {
      assert(HEAPU8[allocs[1][i]] === (i & 255));
    }
    for (var i = 0; i < allocs[2].length; i++) {
      assert(HEAPU8[allocs[2][i]] === ((i*i) & 255));
    }
    out('success.');
  }, &x);
}
''')
    for opts in [0, 1, 2]:
      print(opts)
      check_execute([PYTHON, EMCC, '-s', 'WASM=0', 'src.c', '-s', 'SPLIT_MEMORY=8388608', '-s', 'TOTAL_MEMORY=64MB', '-O' + str(opts)])
      self.assertContained('success.', run_js('a.out.js'))

  def test_split_memory_2(self): # make allocation starts in the first chunk, and moves forward properly
    open('src.c', 'w').write(r'''
#include <emscripten.h>
#include <stdlib.h>
#include <assert.h>
int split_memory = 0;
int alloc_where_is_it() {
  static void *last;
  void *ptr = malloc(1024);
  static int counter = 0;
  if (last && (counter++ % 2 == 1)) ptr = realloc(last, 512*1024); // throw in some reallocs of a previous allocation
  last = ptr;
  unsigned x = (unsigned)ptr;
  return x / split_memory;
}
int main() {
  split_memory = EM_ASM_INT({
    return SPLIT_MEMORY;
  });
  int counter = 0;
  while (alloc_where_is_it() == 0) {
    counter++;
  }
  printf("allocations in first chunk: %d\n", counter);
  assert(counter > 10); // less in first chunk
  while (alloc_where_is_it() == 1) {
    counter++;
  }
  printf("allocations in first chunk: %d\n", counter);
  assert(counter > 20);
  counter = 0;
  while (alloc_where_is_it() == 2) {
    counter++;
  }
  printf("allocations in second chunk: %d\n", counter);
  assert(counter > 20);
  EM_ASM( out('success.') );
}
''')
    for opts in [0, 1, 2]:
      print(opts)
      check_execute([PYTHON, EMCC, '-s', 'WASM=0', 'src.c', '-s', 'SPLIT_MEMORY=8388608', '-s', 'TOTAL_MEMORY=64MB', '-O' + str(opts)])
      self.assertContained('success.', run_js('a.out.js'))

  def test_split_memory_sbrk(self):
    open('src.c', 'w').write(r'''
#include <emscripten.h>
#include <stdlib.h>
#include <unistd.h>
#include <assert.h>
int split_memory;
int where(int x) {
  return x / split_memory;
}
int main() {
  split_memory = EM_ASM_INT({
    return SPLIT_MEMORY;
  });
  int sbrk_0 = (int)sbrk(0);
  printf("sbrk(0): %d\n", sbrk_0);
  assert(sbrk_0 > 0 && sbrk_0 != -1);
  int sbrk_index = where(sbrk_0);
  assert(sbrk_index > 0 && sbrk_index < 10);
  assert(sbrk(0) == (void*)sbrk_0);
  int one = (int)sbrk(10);
  printf("one: %d\n", one);
  assert(sbrk_0 + 10 == sbrk(0));
  int two = (int)sbrk(20);
  printf("two: %d\n", two);
  assert(sbrk_0 + 10 == two);
  assert(sbrk(-20) == (void*)(two + 20));
  assert(sbrk(-10) == (void*)two);
  int bad = sbrk(split_memory * 2);
  assert(bad == -1);
  EM_ASM( out('success.') );
}
''')
    for opts in [0, 1, 2]:
      print(opts)
      check_execute([PYTHON, EMCC, '-s', 'WASM=0', 'src.c', '-s', 'SPLIT_MEMORY=8388608', '-s', 'TOTAL_MEMORY=64MB', '-O' + str(opts)])
      self.assertContained('success.', run_js('a.out.js'))

  def test_split_memory_faking(self): # fake HEAP8 etc. objects have some faked fake method. they are fake
    open('src.c', 'w').write(r'''
#include <emscripten.h>
#include <stdlib.h>
#include <unistd.h>
#include <assert.h>
int main() {
  EM_ASM((
    var x = Module._malloc(1024);
    // set
    HEAPU8.set([1,2,3,4], x);
    assert(get8(x+0) === 1);
    assert(get8(x+1) === 2);
    assert(get8(x+2) === 3);
    assert(get8(x+3) === 4);
    // subarray
    var s1 = HEAPU8.subarray(x+2, x+4);
    assert(s1 instanceof Uint8Array);
    assert(s1.length === 2);
    assert(s1[0] === 3);
    assert(s1[1] === 4);
    assert(get8(x+2) === 3);
    s1[0] = 57;
    assert(get8(x+2) === 57);
    // subarray without second param
    var s2 = HEAPU8.subarray(x+2);
    assert(s2 instanceof Uint8Array);
    assert(s2.length > 2);
    assert(s2[0] === 57);
    assert(s2[1] === 4);
    assert(get8(x+2) === 57);
    s2[0] = 98;
    assert(get8(x+2) === 98);
    // buffer.slice
    var b = HEAPU8.buffer.slice(x, x+4);
    assert(b instanceof ArrayBuffer);
    assert(b.byteLength === 4);
    var s = new Uint8Array(b);
    assert(s[0] === 1);
    assert(s[1] === 2);
    assert(s[2] === 98);
    assert(s[3] === 4);
    // check for a bananabread-discovered bug
    var s32 = HEAPU32.subarray(x >> 2, x + 4 >> 2);
    assert(s32 instanceof Uint32Array);
    assert(s32.length === 1);
    assert(s32[0] === 0x04620201, s32[0]);
    // misc subarrays, check assertions only
    SPLIT_MEMORY = 256;
    SPLIT_MEMORY_BITS = 8;
    function getChunk(x) {
      return x >> SPLIT_MEMORY_BITS;
    }
    assert(TOTAL_MEMORY >= SPLIT_MEMORY*3);
    var p = out;
    var e = err;
    err = out = function(){};
    var fail = false;
    if (!buffers[1]) allocateSplitChunk(1); // we will slice into this
    if (!buffers[2]) allocateSplitChunk(2); // we will slice into this
    TOP:
    for (var i = 0; i < SPLIT_MEMORY*3; i++) {
      HEAPU8.subarray(i);
      if ((i&3) === 0) HEAPU32.subarray(i >> 2);
      for (var j = 1; j < SPLIT_MEMORY*3; j++) {
        //printErr([i, j]);
        if (getChunk(i) == getChunk(j-1) || j <= i) {
          HEAPU8.subarray(i, j);
          if ((i&3) === 0) HEAPU32.subarray(i >> 2, j >> 2);
        } else {
          // expect failures
          try {
            HEAPU8.subarray(i, j);
            fail = ['U8', i, j];
            break TOP;
          } catch (e) {}
          if ((i&3) === 0 && (j&3) === 0) {
            try {
              HEAPU32.subarray(i >> 2, j >> 2);
              fail = ['U32', i, j];
              break TOP;
            } catch (e) {}
          }
          break; // stop inner loop, once we saw different chunks, go to a new i
        }
      }
    }
    out = p;
    err = e;
    if (fail) out('FAIL. ' + fail);
    else out('success.');
  ));
}
''')
    for opts in [0, 1, 2]:
      print(opts)
      check_execute([PYTHON, EMCC, '-s', 'WASM=0', 'src.c', '-s', 'SPLIT_MEMORY=8388608', '-s', 'TOTAL_MEMORY=64MB', '-O' + str(opts), '-s', 'ASSERTIONS=1'])
      self.assertContained('success.', run_js('a.out.js', stderr=PIPE, assert_returncode=None))

  @no_wasm_backend()
  def test_split_memory_release(self):
    open('src.c', 'w').write(r'''
#include <emscripten.h>
#include <stdlib.h>
#include <unistd.h>
#include <assert.h>
int main() {
  EM_ASM({
    assert(buffers[0]); // always here
    assert(!buffers[1]);
    assert(!buffers[2]);
    function getIndex(x) {
      return x >> SPLIT_MEMORY_BITS;
    }
    do {
      var t = Module._malloc(1024*1024);
      out('allocating, got in ' + getIndex(t));
    } while (getIndex(t) === 0);
    assert(getIndex(t) === 1, 'allocated into first chunk');
    assert(buffers[1]); // has been allocated now
    do {
      var t = Module._malloc(1024*1024);
      out('allocating, got in ' + getIndex(t));
    } while (getIndex(t) === 1);
    assert(getIndex(t) === 2, 'allocated into second chunk');
    assert(buffers[2]); // has been allocated now
    Module._free(t);
    assert(!buffers[2]); // has been freed now
    var more = [];
    for (var i = 0 ; i < 1024; i++) {
      more.push(Module._malloc(10));
    }
    assert(buffers[2]); // has been allocated again
    for (var i = 0 ; i < 1024; i++) {
      Module._free(more[i]);
    }
    assert(!buffers[2]); // has been freed again
    out('success.');
  });
}
''')
    for opts in [0, 1, 2]:
      print(opts)
      check_execute([PYTHON, EMCC, '-s', 'WASM=0', 'src.c', '-s', 'SPLIT_MEMORY=8388608', '-s', 'TOTAL_MEMORY=64MB', '-O' + str(opts)])
      self.assertContained('success.', run_js('a.out.js'))

  @no_wasm_backend()
  def test_split_memory_use_existing(self):
    open('src.c', 'w').write(r'''
#include <emscripten.h>
#include <stdlib.h>
#include <unistd.h>
#include <assert.h>
int main() {
  EM_ASM({
    function getIndex(x) {
      return x >> SPLIT_MEMORY_BITS;
    }
    var t;
    do {
      t = Module._malloc(1024*1024);
    } while (getIndex(t) === 0);
    assert(getIndex(t) === 1, 'allocated into first chunk');
    assert(!buffers[2]);
    var existing = new Uint8Array(1024); // ok to be smaller
    allocateSplitChunk(2, existing.buffer);
    assert(buffers[2]);
    existing[0] = 12;
    existing[50] = 98;
    var p = SPLIT_MEMORY*2;
    assert(HEAPU8[p+0] === 12 && HEAPU8[p+50] === 98); // mapped into the normal memory space!
    HEAPU8[p+33] = 201;
    assert(existing[33] === 201); // works both ways
    do {
      t = Module._malloc(1024*1024);
    } while (getIndex(t) === 1);
    assert(getIndex(t) === 3, 'should skip chunk 2, since it is used by us, but seeing ' + getIndex(t));
    assert(HEAPU8[p+0] === 12 && HEAPU8[p+50] === 98);
    assert(existing[33] === 201);
    out('success.');
  });
}
''')
    for opts in [0, 1, 2]:
      print(opts)
      check_execute([PYTHON, EMCC, '-s', 'WASM=0', 'src.c', '-s', 'SPLIT_MEMORY=8388608', '-s', 'TOTAL_MEMORY=64MB', '-O' + str(opts)])
      self.assertContained('success.', run_js('a.out.js'))

  def test_sixtyfour_bit_return_value(self):
    # This test checks that the most significant 32 bits of a 64 bit long are correctly made available
    # to native JavaScript applications that wish to interact with compiled code returning 64 bit longs.
    # The MS 32 bits should be available in Runtime.getTempRet0() even when compiled with -O2 --closure 1

    # Compile test.c and wrap it in a native JavaScript binding so we can call our compiled function from JS.
    check_execute([PYTHON, EMCC, path_from_root('tests', 'return64bit', 'test.c'), '--pre-js', path_from_root('tests', 'return64bit', 'testbindstart.js'), '--pre-js', path_from_root('tests', 'return64bit', 'testbind.js'), '--post-js', path_from_root('tests', 'return64bit', 'testbindend.js'), '-s', 'EXPORTED_FUNCTIONS=["_test"]', '-o', 'test.js', '-O2', '--closure', '1', '-g1', '-s', 'BINARYEN_ASYNC_COMPILATION=0'])

    # Simple test program to load the test.js binding library and call the binding to the
    # C function returning the 64 bit long.
    open(os.path.join(self.get_dir(), 'testrun.js'), 'w').write('''
      var test = require("./test.js");
      test.runtest();
    ''')

    # Run the test and confirm the output is as expected.
    out = run_js('testrun.js', full_output=True)
    assert "low = 5678" in out
    assert "high = 1234" in out

  def test_lib_include_flags(self):
    process = Popen([PYTHON, EMCC] + '-l m -l c -I'.split() + [path_from_root('tests', 'include_test'), path_from_root('tests', 'lib_include_flags.c')], stdout=PIPE, stderr=PIPE)
    process.communicate()
    assert process.returncode is 0, 'Empty -l/-L/-I flags should read the next arg as a param'

  def test_dash_s(self):
    print(check_execute([PYTHON, EMCC, path_from_root('tests', 'hello_world.cpp'), '-s', '-std=c++03']))
    self.assertContained('hello, world!', run_js('a.out.js'))

  def test_dash_s_response_file_string(self):
    open('response_file', 'w').write('"MyModule"\n')
    response_file = os.path.join(os.getcwd(), "response_file")
    print(check_execute([PYTHON, EMCC, path_from_root('tests', 'hello_world.cpp'), '-s', 'EXPORT_NAME=@%s' % response_file]))

  def test_dash_s_response_file_list(self):
    open('response_file', 'w').write('["_main", "_malloc"]\n')
    response_file = os.path.join(os.getcwd(), "response_file")
    print(check_execute([PYTHON, EMCC, path_from_root('tests', 'hello_world.cpp'), '-s', 'EXPORTED_FUNCTIONS=@%s' % response_file, '-std=c++03']))

  def test_dash_s_unclosed_quote(self):
    # Unclosed quote
    err = run_process([PYTHON, EMCC, path_from_root('tests', 'hello_world.cpp'), "-s", "TEST_KEY='MISSING_QUOTE"], stderr=PIPE, check=False).stderr
    self.assertNotContained('AssertionError', err) # Do not mention that it is an assertion error
    self.assertContained('unclosed opened quoted string. expected final character to be "\'"', err)

  def test_dash_s_single_quote(self):
    # Only one quote
    err = run_process([PYTHON, EMCC, path_from_root('tests', 'hello_world.cpp'), "-s", "TEST_KEY='"], stderr=PIPE, check=False).stderr
    self.assertNotContained('AssertionError', err) # Do not mention that it is an assertion error
    self.assertContained('unclosed opened quoted string.', err)

  def test_dash_s_unclosed_list(self):
    # Unclosed list
    err = run_process([PYTHON, EMCC, path_from_root('tests', 'hello_world.cpp'), "-s", "TEST_KEY=[Value1, Value2"], stderr=PIPE, check=False).stderr
    self.assertNotContained('AssertionError', err) # Do not mention that it is an assertion error
    self.assertContained('unclosed opened string list. expected final character to be "]"', err)

  def test_dash_s_valid_list(self):
    err = run_process([PYTHON, EMCC, path_from_root('tests', 'hello_world.cpp'), "-s", "TEST_KEY=[Value1, \"Value2\"]"], stderr=PIPE, check=False).stderr
    self.assertNotContained('a problem occured in evaluating the content after a "-s", specifically', err)

  def test_python_2_3(self): # check emcc/em++ can be called by any python
    # remove .py from EMCC(=emcc.py)
    def trim_py_suffix(filename):
      return filename[:-3] if filename.endswith('.py') else filename

    print()
    for python in ['python', 'python2', 'python3']:
      try:
        check_execute([python, '--version'])
        has = True
      except:
        has = False
      print(python, has)
      if has:
        print('  checking emcc...')
        check_execute([python, trim_py_suffix(EMCC), '--version'])
        print('  checking em++...')
        check_execute([python, trim_py_suffix(EMXX), '--version'])
        if python == 'python2':
          print('  checking emcc.py...')
          check_execute([python, EMCC, '--version'])

  def test_zeroinit(self):
    open('src.c', 'w').write(r'''
#include <stdio.h>
int buf[1048576];
int main() {
  printf("hello, world! %d\n", buf[123456]);
  return 0;
}
''')
    check_execute([PYTHON, EMCC, 'src.c', '-O2', '-g'])
    size = os.stat('a.out.wasm').st_size
    # size should be much smaller than the size of that zero-initialized buffer
    assert size < (123456 / 2), size

  @no_wasm_backend()
  def test_separate_asm_warning(self):
    # Test that -s PRECISE_F32=2 raises a warning that --separate-asm is implied.
    stderr = run_process([PYTHON, EMCC, path_from_root('tests', 'hello_world.c'), '-s', 'WASM=0', '-s', 'PRECISE_F32=2', '-o', 'a.html'], stderr=PIPE).stderr
    self.assertContained('forcing separate asm output', stderr)

    # Test that -s PRECISE_F32=2 --separate-asm should not post a warning.
    stderr = run_process([PYTHON, EMCC, path_from_root('tests', 'hello_world.c'), '-s', 'WASM=0', '-s', 'PRECISE_F32=2', '-o', 'a.html', '--separate-asm'], stderr=PIPE).stderr
    self.assertNotContained('forcing separate asm output', stderr)

    # Test that -s PRECISE_F32=1 should not post a warning.
    stderr = run_process([PYTHON, EMCC, path_from_root('tests', 'hello_world.c'), '-s', 'WASM=0', '-s', 'PRECISE_F32=1', '-o', 'a.html'], stderr=PIPE).stderr
    self.assertNotContained('forcing separate asm output', stderr)

    # Manually doing separate asm should show a warning, if not targeting html
    warning = '--separate-asm works best when compiling to HTML'
    stderr = run_process([PYTHON, EMCC, path_from_root('tests', 'hello_world.c'), '-s', 'WASM=0', '--separate-asm'], stderr=PIPE).stderr
    self.assertContained(warning, stderr)
    stderr = run_process([PYTHON, EMCC, path_from_root('tests', 'hello_world.c'), '-s', 'WASM=0', '--separate-asm', '-o', 'a.html'], stderr=PIPE).stderr
    self.assertNotContained(warning, stderr)

    # test that the warning can be suppressed
    stderr = run_process([PYTHON, EMCC, path_from_root('tests', 'hello_world.c'), '-s', 'WASM=0', '--separate-asm', '-Wno-separate-asm'], stderr=PIPE).stderr
    self.assertNotContained(warning, stderr)

  def test_canonicalize_nan_warning(self):
    open('src.cpp', 'w').write(r'''
#include <stdio.h>

union U {
  int x;
  float y;
} a;


int main() {
  a.x = 0x7FC01234;
  printf("%f\n", a.y);
}
''')

    stderr = run_process([PYTHON, EMCC, 'src.cpp', '-O1'], stderr=PIPE).stderr
    self.assertContained("emcc: warning: cannot represent a NaN literal", stderr)

    stderr = run_process([PYTHON, EMCC, 'src.cpp', '-O1', '-g'], stderr=PIPE).stderr
    self.assertContained("emcc: warning: cannot represent a NaN literal", stderr)
    self.assertContained('//@line 12 "src.cpp"', stderr)

  @no_wasm_backend()
  def test_only_my_code(self):
    check_execute([PYTHON, EMCC, '-O1', path_from_root('tests', 'hello_world.c'), '--separate-asm', '-s', 'WASM=0'])
    count = open('a.out.asm.js').read().count('function ')
    assert count > 30, count # libc brings in a bunch of stuff

    def test(filename, opts, expected_funcs, expected_vars):
      print(filename, opts)
      check_execute([PYTHON, EMCC, path_from_root('tests', filename), '--separate-asm', '-s', 'ONLY_MY_CODE=1', '-s', 'WASM=0'] + opts)
      module = open('a.out.asm.js').read()
      open('asm.js', 'w').write('var Module = {};\n' + module)
      funcs = module.count('function ')
      vars_ = module.count('var ')
      self.assertEqual(funcs, expected_funcs)
      self.assertEqual(vars_, expected_vars)
      if SPIDERMONKEY_ENGINE in JS_ENGINES:
        out = run_js('asm.js', engine=SPIDERMONKEY_ENGINE, stderr=STDOUT)
        self.validate_asmjs(out)
      else:
        print('(skipping asm.js validation check)')

    test('hello_123.c', ['-O1'], 1, 2)
    test('fasta.cpp', ['-O3', '-g2'], 2, 12)

  def test_link_response_file_does_not_force_absolute_paths(self):
    with_space = 'with space'
    directory_with_space_name = os.path.join(self.get_dir(), with_space)
    if not os.path.exists(directory_with_space_name):
      os.makedirs(directory_with_space_name)

    main = '''
      int main() {
        return 0;
      }
    '''
    main_file_name = 'main.cpp'
    main_path_name = os.path.join(directory_with_space_name, main_file_name)
    open(main_path_name, 'w').write(main)
    main_object_file_name = 'main.cpp.o'

    Building.emcc(main_path_name, ['-g'])

    current_directory = os.getcwd()
    os.chdir(os.path.join(current_directory, with_space))

    link_args = Building.link([main_object_file_name], os.path.join(self.get_dir(), 'all.bc'), just_calculate=True)

    # Move away from the created temp directory and remove it
    os.chdir(current_directory)
    time.sleep(0.2) # Wait for Windows FS to release access to the directory
    shutil.rmtree(os.path.join(current_directory, with_space))

    # We want only the relative path to be in the linker args, it should not be converted to an absolute path.
    if hasattr(self, 'assertCountEqual'):
      self.assertCountEqual(link_args, [main_object_file_name])
    else:
      # Python 2 compatibility
      self.assertItemsEqual(link_args, [main_object_file_name])

  def test_memory_growth_noasm(self):
    check_execute([PYTHON, EMCC, path_from_root('tests', 'hello_world.c'), '-O2', '-s', 'ALLOW_MEMORY_GROWTH=1'])
    src = open('a.out.js').read()
    assert 'use asm' not in src

  def test_EM_ASM_i64(self):
    open('src.cpp', 'w').write('''
#include <stdint.h>
#include <emscripten.h>

int main() {
  EM_ASM({
    out('inputs: ' + $0 + ', ' + $1 + '.');
  }, int64_t(0x12345678ABCDEF1FLL));
}
''')
    err = run_process([PYTHON, EMCC, 'src.cpp', '-Oz'], stderr=PIPE, check=False).stderr
    self.assertContained('EM_ASM should not receive i64s as inputs, they are not valid in JS', err)

  @unittest.skipIf('EMCC_DEBUG' in os.environ, 'cannot run in debug mode')
  @no_wasm_backend('EVAL_CTORS does not work with wasm backend')
  def test_eval_ctors(self):
    for wasm in (1, 0):
      print('wasm', wasm)
      print('non-terminating ctor')
      src = r'''
        struct C {
          C() {
            volatile int y = 0;
            while (y == 0) {}
          }
        };
        C always;
        int main() {}
      '''
      open('src.cpp', 'w').write(src)
      run_process([PYTHON, EMCC, 'src.cpp', '-O2', '-s', 'EVAL_CTORS=1', '-profiling-funcs', '-s', 'WASM=%d' % wasm])
      print('check no ctors is ok')
      run_process([PYTHON, EMCC, path_from_root('tests', 'hello_world.cpp'), '-Oz', '-s', 'WASM=%d' % wasm])
      self.assertContained('hello, world!', run_js('a.out.js'))

      # on by default in -Oz, but user-overridable
      def get_size(args):
        print('get_size', args)
        check_execute([PYTHON, EMCC, path_from_root('tests', 'hello_libcxx.cpp'), '-s', 'WASM=%d' % wasm] + args)
        self.assertContained('hello, world!', run_js('a.out.js'))
        if wasm:
          codesize = self.count_wasm_contents('a.out.wasm', 'funcs')
          memsize = self.count_wasm_contents('a.out.wasm', 'memory-data')
        else:
          codesize = os.path.getsize('a.out.js')
          memsize = os.path.getsize('a.out.js.mem')
        return (codesize, memsize)

      def check_size(left, right):
        # can't measure just the mem out of the wasm, so ignore [1] for wasm
        if left[0] == right[0] and left[1] == right[1]: return 0
        if left[0] < right[0] and left[1] > right[1]: return -1 # smaller code, bigger mem
        if left[0] > right[0] and left[1] < right[1]: return 1
        assert 0, [left, right]

      o2_size = get_size(['-O2'])
      assert check_size(get_size(['-O2']), o2_size) == 0, 'deterministic'
      assert check_size(get_size(['-O2', '-s', 'EVAL_CTORS=1']), o2_size) < 0, 'eval_ctors works if user asks for it'
      oz_size = get_size(['-Oz'])
      assert check_size(get_size(['-Oz']), oz_size) == 0, 'deterministic'
      assert check_size(get_size(['-Oz', '-s', 'EVAL_CTORS=1']), oz_size) == 0, 'eval_ctors is on by default in oz'
      assert check_size(get_size(['-Oz', '-s', 'EVAL_CTORS=0']), oz_size) == 1, 'eval_ctors can be turned off'

      linkable_size =   get_size(['-Oz', '-s', 'EVAL_CTORS=1', '-s', 'LINKABLE=1'])
      assert check_size(get_size(['-Oz', '-s', 'EVAL_CTORS=0', '-s', 'LINKABLE=1']), linkable_size) == 1, 'noticeable difference in linkable too'

      # ensure order of execution remains correct, even with a bad ctor
      def test(p1, p2, p3, last, expected):
        src = r'''
          #include <stdio.h>
          #include <stdlib.h>
          volatile int total = 0;
          struct C {
            C(int x) {
              volatile int y = x;
              y++;
              y--;
              if (y == 0xf) {
                printf("you can't eval me ahead of time\n"); // bad ctor
              }
              total <<= 4;
              total += int(y);
            }
          };
          C __attribute__((init_priority(%d))) c1(0x5);
          C __attribute__((init_priority(%d))) c2(0x8);
          C __attribute__((init_priority(%d))) c3(%d);
          int main() {
            printf("total is 0x%%x.\n", total);
          }
        ''' % (p1, p2, p3, last)
        open('src.cpp', 'w').write(src)
        run_process([PYTHON, EMCC, 'src.cpp', '-O2', '-s', 'EVAL_CTORS=1', '-profiling-funcs', '-s', 'WASM=%d' % wasm])
        self.assertContained('total is %s.' % hex(expected), run_js('a.out.js'))
        shutil.copyfile('a.out.js', 'x' + hex(expected) + '.js')
        if wasm:
          shutil.copyfile('a.out.wasm', 'x' + hex(expected) + '.wasm')
          return self.count_wasm_contents('a.out.wasm', 'funcs')
        else:
          return open('a.out.js').read().count('function _')
      print('no bad ctor')
      first  = test(1000, 2000, 3000, 0xe, 0x58e)
      second = test(3000, 1000, 2000, 0xe, 0x8e5)
      third  = test(2000, 3000, 1000, 0xe, 0xe58)
      print(first, second, third)
      assert first == second and second == third
      print('with bad ctor')
      first  = test(1000, 2000, 3000, 0xf, 0x58f) # 2 will succeed
      second = test(3000, 1000, 2000, 0xf, 0x8f5) # 1 will succedd
      third  = test(2000, 3000, 1000, 0xf, 0xf58) # 0 will succeed
      print(first, second, third)
      assert first < second and second < third, [first, second, third]

      print('helpful output')
      try:
        os.environ['EMCC_DEBUG'] = '1'
        open('src.cpp', 'w').write(r'''
  #include <stdio.h>
  struct C {
    C() { printf("constructing!\n"); } // don't remove this!
  };
  C c;
  int main() {}
        ''')
        with clean_write_access_to_canonical_temp_dir(self.canonical_temp_dir):
          err = run_process([PYTHON, EMCC, 'src.cpp', '-Oz', '-s', 'WASM=%d' % wasm], stderr=PIPE).stderr
        self.assertContained('___syscall54', err) # the failing call should be mentioned
        if not wasm: # js will show a stack trace
          self.assertContained('ctorEval.js', err) # with a stack trace
        self.assertContained('ctor_evaller: not successful', err) # with logging
      finally:
        del os.environ['EMCC_DEBUG']

  def test_override_environment(self):
    open('main.cpp', 'w').write(r'''
      #include <emscripten.h>
      int main() {
        EM_ASM({
          out('environment is WEB? ' + ENVIRONMENT_IS_WEB);
          out('environment is WORKER? ' + ENVIRONMENT_IS_WORKER);
          out('environment is NODE? ' + ENVIRONMENT_IS_NODE);
          out('environment is SHELL? ' + ENVIRONMENT_IS_SHELL);
        });
      }
''')
    # use SINGLE_FILE since we don't want to depend on loading a side .wasm file on the environment in this test;
    # with the wrong env we have very odd failures
    check_execute([PYTHON, EMCC, 'main.cpp', '-s', 'SINGLE_FILE=1'])
    src = open('a.out.js').read()
    envs = ['web', 'worker', 'node', 'shell']
    for env in envs:
      for engine in JS_ENGINES:
        if engine == V8_ENGINE: continue # ban v8, weird failures
        actual = 'NODE' if engine == NODE_JS else 'SHELL'
        print(env, actual, engine)
        module = { 'ENVIRONMENT': env }
        if env != actual:
          # avoid problems with arguments detection, which may cause very odd failures with the wrong environment code
          module['arguments'] = []
        curr = 'var Module = %s;\n' % str(module)
        print('    ' + curr)
        open('test.js', 'w').write(curr + src)
        fail = False
        seen = run_js('test.js', engine=engine, stderr=PIPE, full_output=True, assert_returncode=None)
        self.assertContained('Module.ENVIRONMENT has been deprecated. To force the environment, use the ENVIRONMENT compile-time option (for example, -s ENVIRONMENT=web or -s ENVIRONMENT=node', seen)

  def test_warn_no_filesystem(self):
    WARNING = 'Filesystem support (FS) was not included. The problem is that you are using files from JS, but files were not used from C/C++, so filesystem support was not auto-included. You can force-include filesystem support with  -s FORCE_FILESYSTEM=1'

    check_execute([PYTHON, EMCC, path_from_root('tests', 'hello_world.c')])
    seen = run_js('a.out.js', stderr=PIPE)
    assert WARNING not in seen

    def test(contents):
      open('src.cpp', 'w').write(r'''
  #include <stdio.h>
  #include <emscripten.h>
  int main() {
    EM_ASM({ %s });
    printf("hello, world!\n");
    return 0;
  }
  ''' % contents)
      check_execute([PYTHON, EMCC, 'src.cpp'])
      self.assertContained(WARNING, run_js('a.out.js', stderr=PIPE, assert_returncode=None))

    # might appear in handwritten code
    test("FS.init()")
    test("FS.createPreloadedFile('waka waka, just warning check')");
    test("FS.createDataFile('waka waka, just warning check')");
    test("FS.analyzePath('waka waka, just warning check')");
    test("FS.loadFilesFromDB('waka waka, just warning check')");
    # might appear in filesystem code from a separate script tag
    test("Module['FS_createDataFile']('waka waka, just warning check')");
    test("Module['FS_createPreloadedFile']('waka waka, just warning check')");

    # text is in the source when needed, but when forcing FS, it isn't there
    check_execute([PYTHON, EMCC, 'src.cpp'])
    self.assertContained(WARNING, open('a.out.js').read())
    check_execute([PYTHON, EMCC, 'src.cpp', '-s', 'FORCE_FILESYSTEM=1']) # forcing FS means no need
    self.assertNotContained(WARNING, open('a.out.js').read())
    check_execute([PYTHON, EMCC, 'src.cpp', '-s', 'ASSERTIONS=0']) # no assertions, no need
    self.assertNotContained(WARNING, open('a.out.js').read())
    check_execute([PYTHON, EMCC, 'src.cpp', '-O2']) # optimized, so no assertions
    self.assertNotContained(WARNING, open('a.out.js').read())

  def test_warn_module_print_err(self):
    ERROR = 'was not exported. add it to EXTRA_EXPORTED_RUNTIME_METHODS (see the FAQ)'

    def test(contents, expected, args=[]):
      open('src.cpp', 'w').write(r'''
  #include <emscripten.h>
  int main() {
    EM_ASM({ %s });
    return 0;
  }
  ''' % contents)
      run_process([PYTHON, EMCC, 'src.cpp'] + args)
      self.assertContained(expected, run_js('a.out.js', stderr=STDOUT, assert_returncode=None))

    # error shown (when assertions are on)
    test("Module.print('x')", ERROR)
    test("Module['print']('x')", ERROR)
    test("Module.printErr('x')", ERROR)
    test("Module['printErr']('x')", ERROR)

    # when exported, all good
    test("Module['print']('print'); Module['printErr']('err'); ", 'print\nerr', ['-s', 'EXTRA_EXPORTED_RUNTIME_METHODS=["print", "printErr"]'])

  def test_arc4random(self):
    open('src.c', 'w').write(r'''
#include <stdlib.h>
#include <stdio.h>

int main() {
  printf("%d\n", arc4random());
  printf("%d\n", arc4random());
}
    ''')
    check_execute([PYTHON, EMCC, 'src.c', '-Wno-implicit-function-declaration'])

    self.assertContained('0\n740882966\n', run_js('a.out.js'))

  ############################################################
  # Function eliminator tests
  ############################################################
  def normalize_line_endings(self, input):
    return input.replace('\r\n', '\n').replace('\n\n', '\n').replace('\n\n', '\n')

  def get_file_contents(self, file):
    file_contents = ""
    with open(file) as fout:
      file_contents = "".join(fout.readlines())

    file_contents = self.normalize_line_endings(file_contents)

    return file_contents

  def function_eliminator_test_helper(self, input_file, expected_output_file, use_hash_info=False):
    input_file = path_from_root('tests', 'optimizer', input_file)
    expected_output_file = path_from_root('tests', 'optimizer', expected_output_file)
    command = [path_from_root('tools', 'eliminate-duplicate-functions.js'), input_file, '--no-minimize-whitespace', '--use-asm-ast']

    if use_hash_info:
      command.append('--use-hash-info')

    proc = run_process(NODE_JS + command, stdin=PIPE, stderr=PIPE, stdout=PIPE)
    assert proc.stderr == '', proc.stderr
    expected_output = self.get_file_contents(expected_output_file)
    output = self.normalize_line_endings(proc.stdout)

    self.assertIdentical(expected_output, output)

  def test_function_eliminator_simple(self):
    self.function_eliminator_test_helper('test-function-eliminator-simple.js',
                                         'test-function-eliminator-simple-output.js')

  def test_function_eliminator_replace_function_call(self):
    self.function_eliminator_test_helper('test-function-eliminator-replace-function-call.js',
                                        'test-function-eliminator-replace-function-call-output.js')

  def test_function_eliminator_replace_function_call_two_passes(self):
    self.function_eliminator_test_helper('test-function-eliminator-replace-function-call-output.js',
                                         'test-function-eliminator-replace-function-call-two-passes-output.js')

  def test_function_eliminator_replace_array_value(self):
    output_file = 'output.js'

    try:
      tools.shared.safe_copy(path_from_root('tests', 'optimizer', 'test-function-eliminator-replace-array-value.js'), output_file)

      tools.duplicate_function_eliminator.run(output_file)

      output_file_contents = self.get_file_contents(output_file)

      expected_file_contents = self.get_file_contents(path_from_root('tests', 'optimizer', 'test-function-eliminator-replace-array-value-output.js'))

      self.assertIdentical(output_file_contents, expected_file_contents)
    finally:
      tools.tempfiles.try_delete(output_file)

  def test_function_eliminator_replace_object_value_assignment(self):
    self.function_eliminator_test_helper('test-function-eliminator-replace-object-value-assignment.js',
                                         'test-function-eliminator-replace-object-value-assignment-output.js')

  def test_function_eliminator_variable_clash(self):
    self.function_eliminator_test_helper('test-function-eliminator-variable-clash.js',
                                         'test-function-eliminator-variable-clash-output.js')

  def test_function_eliminator_replace_variable_value(self):
    self.function_eliminator_test_helper('test-function-eliminator-replace-variable-value.js',
                                         'test-function-eliminator-replace-variable-value-output.js')

  def test_function_eliminator_double_parsed_correctly(self):
    # This is a test that makes sure that when we perform final optimization on
    # the JS file, doubles are preserved (and not converted to ints).
    output_file = 'output.js'

    try:
      tools.shared.safe_copy(path_from_root('tests', 'optimizer', 'test-function-eliminator-double-parsed-correctly.js'), output_file)

      # Run duplicate function elimination
      tools.duplicate_function_eliminator.run(output_file)

      # Run last opts
      shutil.move(tools.js_optimizer.run(output_file, ['last', 'asm']), output_file)
      output_file_contents = self.get_file_contents(output_file)

      # Compare
      expected_file_contents = self.get_file_contents(path_from_root('tests', 'optimizer', 'test-function-eliminator-double-parsed-correctly-output.js'))
      self.assertIdentical(expected_file_contents, output_file_contents)
    finally:
      tools.tempfiles.try_delete(output_file)

  # Now do the same, but using a pre-generated equivalent function hash info that
  # comes in handy for parallel processing
  def test_function_eliminator_simple_with_hash_info(self):
    self.function_eliminator_test_helper('test-function-eliminator-simple-with-hash-info.js',
                                         'test-function-eliminator-simple-output.js',
                                         use_hash_info=True)

  def test_function_eliminator_replace_function_call_with_hash_info(self):
    self.function_eliminator_test_helper('test-function-eliminator-replace-function-call-with-hash-info.js',
                                         'test-function-eliminator-replace-function-call-output.js',
                                         use_hash_info=True)

  def test_function_eliminator_replace_function_call_two_passes_with_hash_info(self):
    self.function_eliminator_test_helper('test-function-eliminator-replace-function-call-output-with-hash-info.js',
                                         'test-function-eliminator-replace-function-call-two-passes-output.js',
                                         use_hash_info=True)

  def test_function_eliminator_replace_object_value_assignment_with_hash_info(self):
    self.function_eliminator_test_helper('test-function-eliminator-replace-object-value-assignment-with-hash-info.js',
                                         'test-function-eliminator-replace-object-value-assignment-output.js',
                                         use_hash_info=True)

  def test_function_eliminator_variable_clash_with_hash_info(self):
    self.function_eliminator_test_helper('test-function-eliminator-variable-clash-with-hash-info.js',
                                         'test-function-eliminator-variable-clash-output.js',
                                         use_hash_info=True)

  def test_function_eliminator_replace_variable_value_with_hash_info(self):
    self.function_eliminator_test_helper('test-function-eliminator-replace-variable-value-with-hash-info.js',
                                         'test-function-eliminator-replace-variable-value-output.js',
                                         use_hash_info=True)

  @no_wasm_backend()
  def test_cyberdwarf_pointers(self):
    check_execute([PYTHON, EMCC, path_from_root('tests', 'debugger', 'test_pointers.cpp'), '-Oz', '-s', 'CYBERDWARF=1',
    '-std=c++11', '--pre-js', path_from_root('tests', 'debugger', 'test_preamble.js'), '-o', 'test_pointers.js' ], stderr=PIPE)
    run_js('test_pointers.js', engine=NODE_JS)

  @no_wasm_backend()
  def test_cyberdwarf_union(self):
    check_execute([PYTHON, EMCC, path_from_root('tests', 'debugger', 'test_union.cpp'), '-Oz', '-s', 'CYBERDWARF=1',
    '-std=c++11', '--pre-js', path_from_root('tests', 'debugger', 'test_preamble.js'), '-o', 'test_union.js' ])
    run_js('test_union.js', engine=NODE_JS)

  def test_source_file_with_fixed_language_mode(self):
    open('src_tmp_fixed_lang', 'w').write('''
#include <string>
#include <iostream>

int main() {
  std::cout << "Test_source_fixed_lang_hello" << std::endl;
  return 0;
}
    ''')
    stderr = run_process([PYTHON, EMCC, '-Wall', '-std=c++14', '-x', 'c++', 'src_tmp_fixed_lang'], stderr=PIPE).stderr
    self.assertNotContained("Input file has an unknown suffix, don't know what to do with it!", stderr)
    self.assertNotContained("Unknown file suffix when compiling to LLVM bitcode", stderr)
    self.assertContained("Test_source_fixed_lang_hello", run_js('a.out.js'))

    stderr = run_process([PYTHON, EMCC, '-Wall', '-std=c++14', 'src_tmp_fixed_lang'], stderr=PIPE, check=False).stderr
    self.assertContained("Input file has an unknown suffix, don't know what to do with it!", stderr)

  def test_disable_inlining(self):
    open('test.c', 'w').write(r'''
#include <stdio.h>

void foo() {
  printf("foo\n");
}

int main() {
  foo();
  return 0;
}
''')
    # Without the 'INLINING_LIMIT=1', -O2 inlines foo()
    check_execute([PYTHON, EMCC, 'test.c', '-O2', '-o', 'test.bc', '-s', 'INLINING_LIMIT=1'])
    # If foo() had been wrongly inlined above, internalizing foo and running
    # global DCE makes foo DCE'd
    Building.llvm_opt('test.bc', ['-internalize', '-internalize-public-api-list=main', '-globaldce'], 'test.bc')

    # To this test to be successful, foo() shouldn't have been inlined above and
    # foo() should be in the function list
    syms = Building.llvm_nm('test.bc', include_internal=True)
    assert 'foo' in syms.defs, 'foo() should not be inlined'
    try_delete('test.c')
    try_delete('test.bc')

  @no_wasm_backend()
  def test_output_eol(self):
    # --separate-asm only makes sense without wasm (no asm.js with wasm)
    for params in [[], ['--separate-asm', '-s', 'WASM=0'], ['--proxy-to-worker'], ['--proxy-to-worker', '--separate-asm', '-s', 'WASM=0']]:
      for output_suffix in ['html', 'js']:
        for eol in ['windows', 'linux']:
          files = ['a.js']
          if '--separate-asm' in params: files += ['a.asm.js']
          if output_suffix == 'html': files += ['a.html']
          cmd = [PYTHON, EMCC, path_from_root('tests', 'hello_world.c'), '-o', 'a.' + output_suffix, '--output_eol', eol] + params
          print(cmd)
          run_process(cmd)
          for f in files:
            print(str(cmd) + ' ' + str(params) + ' ' + eol + ' ' + f)
            assert os.path.isfile(f)
            ret = tools.line_endings.check_line_endings(f, expect_only_specific_line_endings='\n' if eol == 'linux' else '\r\n')
            assert ret == 0

          for f in files:
            try_delete(f)

  @unittest.skipIf('EMCC_DEBUG' in os.environ, 'cannot run in debug mode')
  @no_wasm_backend('asm2wasm specific')
  def test_binaryen_opts(self):
    with clean_write_access_to_canonical_temp_dir(self.canonical_temp_dir):
      try:
        os.environ['EMCC_DEBUG'] = '1'
        for args, expect_js_opts, expect_only_wasm in [
            ([], False, True),
            (['-O0'], False, True),
            (['-O1'], False, True),
            (['-O2'], False, True),
            (['-O2', '--js-opts', '1'], True, False), # user asked
            (['-O2', '-s', 'EMTERPRETIFY=1'], True, False), # option forced
            (['-O2', '-s', 'EMTERPRETIFY=1', '-s', 'ALLOW_MEMORY_GROWTH=1'], True, False), # option forced, and also check growth does not interfere
            (['-O2', '-s', 'EVAL_CTORS=1'], False, True), # ctor evaller turned off since only-wasm
            (['-O2', '-s', 'OUTLINING_LIMIT=1000'], True, False), # option forced
            (['-O2', '-s', 'OUTLINING_LIMIT=1000', '-s', 'ALLOW_MEMORY_GROWTH=1'], True, False), # option forced, and also check growth does not interfere
            (['-O2', '-s', "BINARYEN_METHOD='interpret-s-expr,asmjs'"], True, False), # asmjs in methods means we need good asm.js
            (['-O3'], False, True),
            (['-Os'], False, True),
            (['-Oz'], False, True), # ctor evaller turned off since only-wasm
          ]:
          try_delete('a.out.js')
          try_delete('a.out.wast')
          cmd = [PYTHON, EMCC, path_from_root('tests', 'core', 'test_i64.c'), '-s', 'BINARYEN_METHOD="interpret-s-expr"'] + args
          print(args, 'js opts:', expect_js_opts, 'only-wasm:', expect_only_wasm, '   ', ' '.join(cmd))
          err = run_process(cmd, stdout=PIPE, stderr=PIPE).stderr
          assert expect_js_opts == ('applying js optimization passes:' in err), err
          if not self.is_wasm_backend():
            assert expect_only_wasm == ('-emscripten-only-wasm' in err and '--wasm-only' in err), err # check both flag to fastcomp and to asm2wasm
          wast = open('a.out.wast').read()
          # i64s
          i64s = wast.count('(i64.')
          print('    seen i64s:', i64s)
          assert expect_only_wasm == (i64s > 30), 'i64 opts can be emitted in only-wasm mode, but not normally' # note we emit a few i64s even without wasm-only, when we replace udivmoddi (around 15 such)
          selects = wast.count('(select')
          print('    seen selects:', selects)
          if '-Os' in args or '-Oz' in args:
            # when optimizing for size we should create selects
            self.assertGreater(selects, 50)
          else:
            # when not optimizing for size we should not create selects
            self.assertLess(selects, 10)
          # asm2wasm opt line
          asm2wasm_line = [line for line in err.split('\n') if 'asm2wasm' in line]
          asm2wasm_line = '' if not asm2wasm_line else asm2wasm_line[0]
          if '-O0' in args or '-O' not in str(args):
            assert '-O' not in asm2wasm_line, 'no opts should be passed to asm2wasm: ' + asm2wasm_line
          else:
            opts_str = args[0]
            assert opts_str.startswith('-O')
            assert opts_str in asm2wasm_line, 'expected opts: ' + asm2wasm_line
      finally:
        del os.environ['EMCC_DEBUG']


  @unittest.skipIf('EMCC_DEBUG' in os.environ, 'cannot run in debug mode')
  @no_wasm_backend()
  def test_binaryen_and_precise_f32(self):
    try:
      os.environ['EMCC_DEBUG'] = '1'
      for args, expect in [
          ([], True),
          (['-s', 'PRECISE_F32=0'], True), # disabled, but no asm.js, so we definitely want f32
          (['-s', 'PRECISE_F32=0', '-s', 'BINARYEN_METHOD="asmjs"'], False), # disabled, and we need the asm.js
          (['-s', 'PRECISE_F32=1'], True),
          (['-s', 'PRECISE_F32=2'], True),
        ]:
        print(args, expect)
        try_delete('a.out.js')
        with clean_write_access_to_canonical_temp_dir(self.canonical_temp_dir):
          err = run_process([PYTHON, EMCC, path_from_root('tests', 'hello_world.cpp'), '-s', 'BINARYEN=1', '-s', 'BINARYEN_METHOD="interpret-binary"'] + args, stdout=PIPE, stderr=PIPE).stderr
        assert expect == (' -emscripten-precise-f32' in err), err
        self.assertContained('hello, world!', run_js('a.out.js'))
    finally:
      del os.environ['EMCC_DEBUG']

  def test_binaryen_names(self):
    sizes = {}
    for args, expect_names in [
        ([], False),
        (['-g'], True),
        (['-O1'], False),
        (['-O2'], False),
        (['-O2', '-g'], True),
        (['-O2', '-g1'], False),
        (['-O2', '-g2'], True),
        (['-O2', '--profiling'], True),
        (['-O2', '--profiling-funcs'], True),
      ]:
      print(args, expect_names)
      try_delete('a.out.js')
      # we use dlmalloc here, as emmalloc has a bunch of asserts that contain the text "malloc" in them, which makes counting harder
      subprocess.check_call([PYTHON, EMCC, path_from_root('tests', 'hello_world.cpp')] + args + ['-s', 'WASM=1', '-s', 'MALLOC="dlmalloc"'])
      code = open('a.out.wasm', 'rb').read()
      if expect_names:
        # name section adds the name of malloc (there is also another one for the export)
        self.assertEqual(code.count(b'malloc'), 2)
      else:
        # should be just one name, for the export
        self.assertEqual(code.count(b'malloc'), 1)
      sizes[str(args)] = os.stat('a.out.wasm').st_size
    print(sizes)
    self.assertLess(sizes["['-O2']"], sizes["['-O2', '--profiling-funcs']"], 'when -profiling-funcs, the size increases due to function names')

  def test_binaryen_warn_mem(self):
    if SPIDERMONKEY_ENGINE not in JS_ENGINES: self.skipTest('cannot run without spidermonkey')
    # if user changes TOTAL_MEMORY at runtime, the wasm module may not accept the memory import if it is too big/small
    open('pre.js', 'w').write('var Module = { TOTAL_MEMORY: 50*1024*1024 };\n')
    subprocess.check_call([PYTHON, EMCC, path_from_root('tests', 'hello_world.cpp'), '-s', 'WASM=1', '-s', 'BINARYEN_METHOD="native-wasm"', '-s', 'TOTAL_MEMORY=' + str(16*1024*1024), '--pre-js', 'pre.js', '-s', 'BINARYEN_ASYNC_COMPILATION=0'])
    out = run_js('a.out.js', engine=SPIDERMONKEY_ENGINE, full_output=True, stderr=PIPE, assert_returncode=None)
    self.assertContained('imported Memory with incompatible size', out)
    self.assertContained('Memory size incompatibility issues may be due to changing TOTAL_MEMORY at runtime to something too large. Use ALLOW_MEMORY_GROWTH to allow any size memory (and also make sure not to set TOTAL_MEMORY at runtime to something smaller than it was at compile time).', out)
    self.assertNotContained('hello, world!', out)
    # and with memory growth, all should be good
    subprocess.check_call([PYTHON, EMCC, path_from_root('tests', 'hello_world.cpp'), '-s', 'WASM=1', '-s', 'BINARYEN_METHOD="native-wasm"', '-s', 'TOTAL_MEMORY=' + str(16*1024*1024), '--pre-js', 'pre.js', '-s', 'ALLOW_MEMORY_GROWTH=1', '-s', 'BINARYEN_ASYNC_COMPILATION=0'])
    self.assertContained('hello, world!', run_js('a.out.js', engine=SPIDERMONKEY_ENGINE))

  def test_binaryen_warn_sync(self):
    if SPIDERMONKEY_ENGINE not in JS_ENGINES: self.skipTest('cannot run without spidermonkey')
    # interpreting will disable async
    for method in ['interpret-binary', 'native-wasm', None]:
      cmd = [PYTHON, EMCC, path_from_root('tests', 'hello_world.cpp'), '-s', 'WASM=1']
      if method is not None:
        cmd += ['-s', 'BINARYEN_METHOD="' + method + '"']
      print(' '.join(cmd))
      err = run_process(cmd, stdout=PIPE, stderr=PIPE).stderr
      print(err)
      warning = 'BINARYEN_ASYNC_COMPILATION disabled due to user options. This will reduce performance and compatibility'
      if method and 'interpret' in method:
        self.assertContained(warning, err)
      else:
        self.assertNotContained(warning, err)

  def test_binaryen_invalid_method(self):
    proc = Popen([PYTHON, EMCC, path_from_root('tests', 'hello_world.cpp'), '-o', 'test.js', '-s', 'WASM=1', '-s', "BINARYEN_METHOD='invalid'"])
    proc.communicate()
    assert proc.returncode != 0

  @no_wasm_backend()
  def test_binaryen_asmjs_outputs(self):
    # Test that an .asm.js file is outputted exactly when it is requested.
    for args, output_asmjs in [
      ([], False),
      (['-s', 'BINARYEN_METHOD="native-wasm"'], False),
      (['-s', 'BINARYEN_METHOD="native-wasm,asmjs"'], True)
    ]:
      with temp_directory() as temp_dir:
        cmd = [PYTHON, EMCC, path_from_root('tests', 'hello_world.c'), '-o', os.path.join(temp_dir, 'a.js')] + args
        print(' '.join(cmd))
        subprocess.check_call(cmd)
        assert os.path.exists(os.path.join(temp_dir, 'a.asm.js')) == output_asmjs
        assert not os.path.exists(os.path.join(temp_dir, 'a.temp.asm.js'))

    # Test that outputting to .wasm does not nuke an existing .asm.js file, if user wants to manually dual-deploy both to same directory.
    with temp_directory() as temp_dir:
      cmd = [PYTHON, EMCC, path_from_root('tests', 'hello_world.c'), '-s', 'WASM=0', '-o', os.path.join(temp_dir, 'a.js'), '--separate-asm']
      print(' '.join(cmd))
      subprocess.check_call(cmd)
      assert os.path.exists(os.path.join(temp_dir, 'a.asm.js'))

      cmd = [PYTHON, EMCC, path_from_root('tests', 'hello_world.c'), '-s', 'WASM=0', '-o', os.path.join(temp_dir, 'a.js'), '-s', 'WASM=1']
      print(' '.join(cmd))
      subprocess.check_call(cmd)
      assert os.path.exists(os.path.join(temp_dir, 'a.asm.js'))
      assert os.path.exists(os.path.join(temp_dir, 'a.wasm'))

      assert not os.path.exists(os.path.join(temp_dir, 'a.temp.asm.js'))

  def test_binaryen_mem(self):
    for args, expect_initial, expect_max in [
        (['-s', 'TOTAL_MEMORY=20971520'], 320, 320),
        (['-s', 'TOTAL_MEMORY=20971520', '-s', 'ALLOW_MEMORY_GROWTH=1'], 320, None),
        (['-s', 'TOTAL_MEMORY=20971520',                                '-s', 'WASM_MEM_MAX=41943040'], 320, 640),
        (['-s', 'TOTAL_MEMORY=20971520', '-s', 'ALLOW_MEMORY_GROWTH=1', '-s', 'WASM_MEM_MAX=41943040'], 320, 640),
      ]:
      cmd = [PYTHON, EMCC, path_from_root('tests', 'hello_world.c'), '-s', 'WASM=1', '-O2', '-s', 'BINARYEN_METHOD="interpret-s-expr"'] + args
      print(' '.join(cmd))
      run_process(cmd)
      for line in open('a.out.wast').readlines():
        if '(import "env" "memory" (memory ' in line:
          parts = line.strip().replace('(', '').replace(')', '').split(' ')
          print(parts)
          self.assertEqual(parts[5], str(expect_initial))
          if not expect_max:
            self.assertEqual(len(parts), 6)
          else:
            self.assertEqual(parts[6], str(expect_max))

  def test_invalid_mem(self):
    # A large amount is fine, multiple of 16MB or not
    run_process([PYTHON, EMCC, path_from_root('tests', 'hello_world.c'), '-s', 'TOTAL_MEMORY=33MB'])
    run_process([PYTHON, EMCC, path_from_root('tests', 'hello_world.c'), '-s', 'TOTAL_MEMORY=32MB'])

    # But not in asm.js
    if not self.is_wasm_backend():
      ret = run_process([PYTHON, EMCC, '-s', 'WASM=0', path_from_root('tests', 'hello_world.c'), '-s', 'TOTAL_MEMORY=33MB'], stderr=subprocess.PIPE, check=False).stderr
      assert 'TOTAL_MEMORY must be a multiple of 16MB' in ret, ret

    # A tiny amount is fine in wasm
    run_process([PYTHON, EMCC, path_from_root('tests', 'hello_world.c'), '-s', 'TOTAL_MEMORY=65536', '-s', 'TOTAL_STACK=1024'])
    # And the program works!
    self.assertContained('hello, world!', run_js('a.out.js'))

    # But not in asm.js
    if not self.is_wasm_backend():
      ret = run_process([PYTHON, EMCC, path_from_root('tests', 'hello_world.c'), '-s', 'TOTAL_MEMORY=65536', '-s', 'WASM=0'], stderr=subprocess.PIPE, check=False).stderr
      assert 'TOTAL_MEMORY must be at least 16MB' in ret, ret

    # Must be a multiple of 64KB
    ret = run_process([PYTHON, EMCC, path_from_root('tests', 'hello_world.c'), '-s', 'TOTAL_MEMORY=32MB+1'], stderr=subprocess.PIPE, check=False).stderr
    assert 'TOTAL_MEMORY must be a multiple of 64KB' in ret, ret

    ret = run_process([PYTHON, EMCC, path_from_root('tests', 'hello_world.c'), '-s', 'WASM_MEM_MAX=33MB'], stderr=subprocess.PIPE, check=False).stderr
    assert 'WASM_MEM_MAX must be a multiple of 64KB' not in ret, ret

    ret = run_process([PYTHON, EMCC, path_from_root('tests', 'hello_world.c'), '-s', 'WASM_MEM_MAX=33MB+1'], stderr=subprocess.PIPE, check=False).stderr
    assert 'WASM_MEM_MAX must be a multiple of 64KB' in ret, ret

  def test_binaryen_ctors(self):
    if SPIDERMONKEY_ENGINE not in JS_ENGINES: self.skipTest('cannot run without spidermonkey')
    # ctor order must be identical to js builds, deterministically
    open('src.cpp', 'w').write(r'''
      #include <stdio.h>
      struct A {
        A() { puts("constructing A!"); }
      };
      A a;
      struct B {
        B() { puts("constructing B!"); }
      };
      B b;
      int main() {}
    ''')
    subprocess.check_call([PYTHON, EMCC, 'src.cpp'])
    correct = run_js('a.out.js', engine=SPIDERMONKEY_ENGINE)
    for args in [[], ['-s', 'RELOCATABLE=1'], ['-s', 'MAIN_MODULE=1']]:
      print(args)
      subprocess.check_call([PYTHON, EMCC, 'src.cpp', '-s', 'WASM=1', '-o', 'b.out.js'] + args)
      seen = run_js('b.out.js', engine=SPIDERMONKEY_ENGINE)
      assert correct == seen, correct + '\n vs \n' + seen

  # test debug info and debuggability of JS output
  @unittest.skipIf('EMCC_DEBUG' in os.environ, 'cannot run in debug mode')
  def test_binaryen_debug(self):
    with clean_write_access_to_canonical_temp_dir(self.canonical_temp_dir):
      try:
        os.environ['EMCC_DEBUG'] = '1'
        for args, expect_dash_g, expect_emit_text, expect_clean_js, expect_whitespace_js, expect_closured in [
            (['-O0'], False, False, False, True, False),
            (['-O0', '-g1'], False, False, False, True, False),
            (['-O0', '-g2'], True, False, False, True, False), # in -g2+, we emit -g to asm2wasm so function names are saved
            (['-O0', '-g'], True, True, False, True, False),
            (['-O0', '--profiling-funcs'], True, False, False, True, False),
            (['-O1'],        False, False, False, True, False),
            (['-O2'],        False, False, True,  False, False),
            (['-O2', '-g1'], False, False, True,  True, False),
            (['-O2', '-g'],  True,  True,  False, True, False),
            (['-O2', '--closure', '1'],         False, False, True, False, True),
            (['-O2', '--closure', '1', '-g1'],  False, False, True, True,  True),
            (['-O2', '--js-opts', '1'], False, False, True,  False, False),
          ]:
          print(args, expect_dash_g, expect_emit_text)
          try_delete('a.out.wast')
          cmd = [PYTHON, EMCC, path_from_root('tests', 'hello_world.cpp'), '-s', 'WASM=1'] + args
          print(' '.join(cmd))
          err = run_process(cmd, stdout=PIPE, stderr=PIPE).stderr
          if not self.is_wasm_backend():
            asm2wasm_line = [x for x in err.split('\n') if 'asm2wasm' in x][0]
            asm2wasm_line = asm2wasm_line.strip() + ' ' # ensure it ends with a space, for simpler searches below
            print('|' + asm2wasm_line + '|')
            assert expect_dash_g == (' -g ' in asm2wasm_line)
            assert expect_emit_text == (' -S ' in asm2wasm_line)
            if expect_emit_text:
              text = open('a.out.wast').read()
              assert ';;' in text, 'must see debug info comment'
              assert 'hello_world.cpp:6' in text, 'must be file:line info'
          js = open('a.out.js').read()
          assert expect_clean_js == ('// ' not in js), 'cleaned-up js must not have comments'
          assert expect_whitespace_js == ('{\n  ' in js), 'whitespace-minified js must not have excess spacing'
          assert expect_closured == ('var a;' in js or 'var a,' in js or 'var a=' in js or 'var a ' in js), 'closured js must have tiny variable names'
      finally:
        del os.environ['EMCC_DEBUG']

  @unittest.skipIf('EMCC_DEBUG' in os.environ, 'cannot run in debug mode')
  @no_wasm_backend()
  def test_binaryen_ignore_implicit_traps(self):
    with clean_write_access_to_canonical_temp_dir(self.canonical_temp_dir):
      sizes = []
      try:
        os.environ['EMCC_DEBUG'] = '1'
        for args, expect in [
            ([], False),
            (['-s', 'BINARYEN_IGNORE_IMPLICIT_TRAPS=1'], True),
          ]:
          print(args, expect)
          cmd = [PYTHON, EMCC, path_from_root('tests', 'hello_libcxx.cpp'), '-s', 'WASM=1', '-O3'] + args
          print(' '.join(cmd))
          err = run_process(cmd, stdout=PIPE, stderr=PIPE).stderr
          asm2wasm_line = [x for x in err.split('\n') if 'asm2wasm' in x][0]
          asm2wasm_line = asm2wasm_line.strip() + ' ' # ensure it ends with a space, for simpler searches below
          print('|' + asm2wasm_line + '|')
          assert expect == (' --ignore-implicit-traps ' in asm2wasm_line)
          sizes.append(os.stat('a.out.wasm').st_size)
      finally:
        del os.environ['EMCC_DEBUG']
      print('sizes:', sizes)
      assert sizes[1] < sizes[0], 'ignoring implicit traps must reduce code size'

  def test_binaryen_methods(self):
    for method_init in ['interpret-asm2wasm', 'interpret-s-expr', 'asmjs', 'interpret-binary', 'asmjs,interpret-binary', 'interpret-binary,asmjs']:
      # check success and failure for simple modes, only success for combined/fallback ones
      for success in [1, 0] if ',' not in method_init else [1]:
        method = method_init
        if self.is_wasm_backend() and ('asmjs' in method or 'asm2wasm' in method):
           continue
        command = [PYTHON, EMCC, '-o', 'a.wasm.js', '-s', 'BINARYEN=1', path_from_root('tests', 'hello_world.c')]
        command += ['-s', 'BINARYEN_METHOD="' + method + '"']
        print(method, ' : ', ' '.join(command), ' => ', success)
        run_process(command)

        see_polyfill =  'var WasmJS = ' in open('a.wasm.js').read()

        if method and 'interpret' not in method:
          assert not see_polyfill, 'verify polyfill was not added - we specified a method, and it does not need it'
        else:
          assert see_polyfill, 'we need the polyfill'

        def break_cashew():
          with open('a.wasm.asm.js') as f: asm = f.read()
          asm = asm.replace('"almost asm"', '"use asm"; var not_in_asm = [].length + (true || { x: 5 }.x);')
          asm = asm.replace("'almost asm'", '"use asm"; var not_in_asm = [].length + (true || { x: 5 }.x);')
          with open('a.wasm.asm.js', 'w') as o: o.write(asm)

        if method.startswith('interpret-asm2wasm'):
          try_delete('a.wasm.wast') # we should not need the .wast
          if not success:
            break_cashew() # we need cashew
        elif method.startswith('interpret-s-expr'):
          try_delete('a.wasm.asm.js') # we should not need the .asm.js
          if not success:
            try_delete('a.wasm.wast')
        elif method.startswith('asmjs'):
          try_delete('a.wasm.wast') # we should not need the .wast
          break_cashew() # we don't use cashew, so ok to break it
          if not success:
            try_delete('a.wasm.js')
        elif method.startswith('interpret-binary'):
          try_delete('a.wasm.wast') # we should not need the .wast
          try_delete('a.wasm.asm.js') # we should not need the .asm.js
          if not success:
            try_delete('a.wasm.wasm')
        else:
          raise Exception('internal test error')

        proc = run_process(NODE_JS + ['a.wasm.js'], stdout=subprocess.PIPE, check=success)
        if success:
          self.assertIn('hello, world!', proc.stdout)
        else:
          assert proc.returncode != 0, proc.stderr
          self.assertNotIn('hello, world!', proc.stdout)

  @no_wasm_backend('contains asm2wasm specifics')
  def test_binaryen_metadce(self):
    def test(filename, expectations):
      sizes = {}
      # in -Os, -Oz, we remove imports wasm doesn't need
      for args, expected_len, expected_exists, expected_not_exists, expected_wasm_size, expected_wasm_imports, expected_wasm_exports in expectations:
        print(args, expected_len, expected_exists, expected_not_exists, expected_wasm_size, expected_wasm_imports, expected_wasm_exports)
        run_process([PYTHON, EMCC, filename] + args + ['-s', 'WASM=1', '-g2'])
        # find the imports we send from JS
        js = open('a.out.js').read()
        start = js.find('Module.asmLibraryArg = ')
        end = js.find('}', start) + 1
        start = js.find('{', start)
        relevant = js[start+2:end-2]
        relevant = relevant.replace(' ', '').replace('"', '').replace("'", '').split(',')
        sent = [x.split(':')[0].strip() for x in relevant]
        sent = [x for x in sent if x]
        sent.sort()
        print('   seen: ' + str(sent))
        for exists in expected_exists:
          self.assertIn(exists, sent)
        for not_exists in expected_not_exists:
          self.assertNotIn(not_exists, sent)
        self.assertEqual(len(sent), expected_len)
        wasm_size = os.stat('a.out.wasm').st_size
        ratio = abs(wasm_size - expected_wasm_size) / float(expected_wasm_size)
        print('  seen wasm size: %d (expected: %d), ratio to expected: %f' % (wasm_size, expected_wasm_size, ratio))
        self.assertLess(ratio, 0.05)
        wast = run_process([os.path.join(Building.get_binaryen_bin(), 'wasm-dis'), 'a.out.wasm'], stdout=PIPE).stdout
        imports = wast.count('(import ')
        exports = wast.count('(export ')
        self.assertEqual(imports, expected_wasm_imports)
        self.assertEqual(exports, expected_wasm_exports)

    print('test on hello world')
    test(path_from_root('tests', 'hello_world.cpp'), [
      ([],      24, ['abort', 'tempDoublePtr'], ['waka'],                  46505,  25,   19),
      (['-O1'], 19, ['abort', 'tempDoublePtr'], ['waka'],                  12630,  16,   17),
      (['-O2'], 19, ['abort', 'tempDoublePtr'], ['waka'],                  12616,  16,   17),
      (['-O3'],  7, ['abort'],                  ['tempDoublePtr', 'waka'],  2818,  10,    2), # in -O3, -Os and -Oz we metadce
      (['-Os'],  7, ['abort'],                  ['tempDoublePtr', 'waka'],  2771,  10,    2),
      (['-Oz'],  7, ['abort'],                  ['tempDoublePtr', 'waka'],  2765,  10,    2),
      # finally, check what happens when we export nothing. wasm should be almost empty
      (['-Os', '-s', 'EXPORTED_FUNCTIONS=[]'],
                 0, [],                         ['tempDoublePtr', 'waka'],     8,   0,    0), # totally empty!
      # but we don't metadce with linkable code! other modules may want it
      (['-O3', '-s', 'MAIN_MODULE=1'],
              1534, ['invoke_i'],               ['waka'],                 469663, 163, 1449),
    ])

    print('test on a minimal pure computational thing')
    open('minimal.c', 'w').write('''
      #include <emscripten.h>

      EMSCRIPTEN_KEEPALIVE
      int add(int x, int y) {
        return x + y;
      }
      ''')
    test('minimal.c', [
      ([],      24, ['abort', 'tempDoublePtr'], ['waka'],                  22712, 25, 18),
      (['-O1'], 12, ['abort', 'tempDoublePtr'], ['waka'],                  10450,  9, 15),
      (['-O2'], 12, ['abort', 'tempDoublePtr'], ['waka'],                  10440,  9, 15),
      # in -O3, -Os and -Oz we metadce, and they shrink it down to the minimal output we want
      (['-O3'],  0, [],                         ['tempDoublePtr', 'waka'],    58,  0,  1),
      (['-Os'],  0, [],                         ['tempDoublePtr', 'waka'],    58,  0,  1),
      (['-Oz'],  0, [],                         ['tempDoublePtr', 'waka'],    58,  0,  1),
    ])

  # ensures runtime exports work, even with metadce
  def test_extra_runtime_exports(self):
    exports = ['stackSave', 'stackRestore', 'stackAlloc']
    run_process([PYTHON, EMCC, path_from_root('tests', 'hello_world.cpp'), '-s', 'WASM=1', '-Os', '-s', 'EXTRA_EXPORTED_RUNTIME_METHODS=%s' % str(exports)])
    js = open('a.out.js').read()
    for export in exports:
      assert ('Module["%s"]' % export) in js, export

  # test disabling of JS FFI legalization
  def test_legalize_js_ffi(self):
    with clean_write_access_to_canonical_temp_dir(self.canonical_temp_dir):
      for (args,js_ffi) in [
          (['-s', 'LEGALIZE_JS_FFI=1', '-s', 'SIDE_MODULE=1', '-O2'], True),
          (['-s', 'LEGALIZE_JS_FFI=0', '-s', 'SIDE_MODULE=1', '-O2'], False),
          (['-s', 'LEGALIZE_JS_FFI=0', '-s', 'SIDE_MODULE=1', '-O0'], False),
          (['-s', 'LEGALIZE_JS_FFI=0', '-s', 'SIDE_MODULE=0', '-O0'], False),
        ]:
        print(args)
        try_delete('a.out.wasm')
        try_delete('a.out.wast')
        cmd = [PYTHON, EMCC, path_from_root('tests', 'other', 'ffi.c'), '-s', 'WASM=1', '-g', '-o', 'a.out.js'] + args
        print(' '.join(cmd))
        proc = Popen(cmd, stdout=PIPE)
        output, err = proc.communicate()
        assert proc.returncode == 0
        text = open('a.out.wast').read()
        # remove internal comments and extra whitespace
        text = re.sub(r'\(;[^;]+;\)', '', text)
        text = re.sub(r' +', ' ', text)
        #print("text: %s" % text)
        e_add_f32 = re.search('func \$_add_f \(param \$*. f32\) \(param \$*. f32\) \(result f32\)', text)
        i_i64_i32 = re.search('import .*"_import_ll" .*\(param i32 i32\) \(result i32\)', text)
        i_f32_f64 = re.search('import .*"_import_f" .*\(param f64\) \(result f64\)', text)
        i_i64_i64 = re.search('import .*"_import_ll" .*\(param i64\) \(result i64\)', text)
        i_f32_f32 = re.search('import .*"_import_f" .*\(param f32\) \(result f32\)', text)
        e_i64_i32 = re.search('func \$_add_ll \(param \$*. i32\) \(param \$*. i32\) \(param \$*. i32\) \(param \$*. i32\) \(result i32\)', text)
        e_f32_f64 = re.search('func \$legalstub\$_add_f \(param \$*. f64\) \(param \$*. f64\) \(result f64\)', text)
        e_i64_i64 = re.search('func \$_add_ll \(param \$*. i64\) \(param \$*. i64\) \(result i64\)', text)
        #print e_add_f32, i_i64_i32, i_f32_f64, i_i64_i64, i_f32_f32, e_i64_i32, e_f32_f64, e_i64_i64
        assert e_add_f32, 'add_f export missing'
        if js_ffi:
          assert i_i64_i32,     'i64 not converted to i32 in imports'
          assert i_f32_f64,     'f32 not converted to f64 in imports'
          assert not i_i64_i64, 'i64 not converted to i32 in imports'
          assert not i_f32_f32, 'f32 not converted to f64 in imports'
          assert e_i64_i32,     'i64 not converted to i32 in exports'
          assert e_f32_f64,     'f32 not converted to f64 in exports'
          assert not e_i64_i64, 'i64 not converted to i32 in exports'
        else:
          assert not i_i64_i32, 'i64 converted to i32 in imports'
          assert not i_f32_f64, 'f32 converted to f64 in imports'
          assert i_i64_i64,     'i64 converted to i32 in imports'
          assert i_f32_f32,     'f32 converted to f64 in imports'
          assert not e_i64_i32, 'i64 converted to i32 in exports'
          assert not e_f32_f64, 'f32 converted to f64 in exports'
          assert e_i64_i64,     'i64 converted to i32 in exports'

  def test_sysconf_phys_pages(self):
    for args, expected in [
        ([], 1024),
        (['-s', 'TOTAL_MEMORY=32MB'], 2048),
        (['-s', 'TOTAL_MEMORY=32MB', '-s', 'ALLOW_MEMORY_GROWTH=1'], (2*1024*1024*1024 - 65536) // 16384),
        (['-s', 'TOTAL_MEMORY=32MB', '-s', 'ALLOW_MEMORY_GROWTH=1', '-s', 'WASM=0'], (2*1024*1024*1024 - 16777216) // 16384),
        (['-s', 'TOTAL_MEMORY=32MB', '-s', 'BINARYEN=1', '-s', 'BINARYEN_METHOD="interpret-asm2wasm"'], 2048),
        (['-s', 'TOTAL_MEMORY=32MB', '-s', 'ALLOW_MEMORY_GROWTH=1', '-s', 'BINARYEN=1', '-s', 'BINARYEN_METHOD="interpret-asm2wasm"'], (2*1024*1024*1024 - 65536) // 16384),
        (['-s', 'TOTAL_MEMORY=32MB', '-s', 'ALLOW_MEMORY_GROWTH=1', '-s', 'BINARYEN=1', '-s', 'BINARYEN_METHOD="interpret-asm2wasm"', '-s', 'WASM_MEM_MAX=128MB'], 2048*4)
      ]:
      cmd = [PYTHON, EMCC, path_from_root('tests', 'unistd', 'sysconf_phys_pages.c')] + args
      print(str(cmd))
      subprocess.check_call(cmd)
      result = run_js('a.out.js').strip()
      print(result)
      assert result == str(expected) + ', errno: 0', expected

  def test_wasm_targets(self):
    for f in ['a.wasm', 'a.wast']:
      process = run_process([PYTHON, EMCC, path_from_root('tests', 'hello_world.cpp'), '-o', f], stdout=PIPE, stderr=PIPE, check=False)
      print(process.stderr)
      assert process.returncode is not 0, 'wasm suffix is an error'
      self.assertContained('output file "%s" has a wasm suffix, but we cannot emit wasm by itself, except as a dynamic library' % f, process.stderr)
    # side modules do allow a wasm target
    for opts, target in [([], 'a.out.wasm'), (['-o', 'lib.wasm'], 'lib.wasm')]:
      # specified target
      print('building: ' + target)
      self.clear()
      run_process([PYTHON, EMCC, path_from_root('tests', 'hello_world.cpp'), '-s', 'WASM=1', '-s', 'SIDE_MODULE=1'] + opts)
      for x in os.listdir('.'):
        assert not x.endswith('.js'), 'we should not emit js when making a wasm side module: ' + x
      self.assertIn(b'dylink', open(target, 'rb').read())

  def test_wasm_backend(self):
    if not has_wasm_target(get_llc_targets()):
      self.skipTest('wasm backend was not built')
    old = os.environ.get('EMCC_WASM_BACKEND')
    if old == '1': return # already the default
    try:
      os.environ['EMCC_WASM_BACKEND'] = '1'
      for args in [[], ['-O1'], ['-O2'], ['-O3'], ['-Os'], ['-Oz']]:
        print(args)
        run_process([PYTHON, EMCC, path_from_root('tests', 'hello_world.cpp')] + args)
        self.assertContained('hello, world!', run_js('a.out.js'))
    finally:
      if not old:
        del os.environ['EMCC_WASM_BACKEND']
      else:
        os.environ['EMCC_WASM_BACKEND'] = old

  def test_wasm_nope(self):
    for opts in [[], ['-O2']]:
      print(opts)
      # check we show a good error message if there is no wasm support
      open('pre.js', 'w').write('WebAssembly = undefined;\n')
      run_process([PYTHON, EMCC, path_from_root('tests', 'hello_world.cpp'), '--pre-js', 'pre.js'] + opts)
      out = run_js('a.out.js', stderr=STDOUT, assert_returncode=None)
      if opts == []:
        self.assertContained('No WebAssembly support found. Build with -s WASM=0 to target JavaScript instead.', out)
      else:
        self.assertContained('no native wasm support detected', out)

  def test_check_engine(self):
    compiler_engine = COMPILER_ENGINE
    bogus_engine = ['/fake/inline4']
    print(compiler_engine)
    jsrun.WORKING_ENGINES = {}
    # Test that engine check passes
    assert jsrun.check_engine(COMPILER_ENGINE)
    # Run it a second time (cache hit)
    assert jsrun.check_engine(COMPILER_ENGINE)
    # Test that engine check fails
    assert not jsrun.check_engine(bogus_engine)
    assert not jsrun.check_engine(bogus_engine)

    # Test the other possible way (list vs string) to express an engine
    if type(compiler_engine) is list:
      engine2 = compiler_engine[0]
    else:
      engine2 = [compiler_engine]
    assert jsrun.check_engine(engine2)

    # Test that run_js requires the engine
    jsrun.run_js(path_from_root('src', 'hello_world.js'), compiler_engine)
    caught_exit = 0
    try:
      jsrun.run_js(path_from_root('src', 'hello_world.js'), bogus_engine)
    except SystemExit as e:
      caught_exit = e.code
    self.assertEqual(1, caught_exit, 'Did not catch SystemExit with bogus JS engine')

  def test_error_on_missing_libraries(self):
    env = os.environ.copy()
    if 'EMCC_STRICT' in env: del env['EMCC_STRICT']

    process = Popen([PYTHON, EMCC, path_from_root('tests', 'hello_world.cpp'), '-lsomenonexistingfile', '-s', 'STRICT=1'], stdout=PIPE, stderr=PIPE, env=env)
    process.communicate()
    assert process.returncode is not 0, '-llsomenonexistingfile is an error in strict mode'

    process = Popen([PYTHON, EMCC, path_from_root('tests', 'hello_world.cpp'), '-lsomenonexistingfile', '-s', 'ERROR_ON_MISSING_LIBRARIES=0'], stdout=PIPE, stderr=PIPE, env=env)
    process.communicate()
    assert process.returncode is 0, '-llsomenonexistingfile is not an error if -s ERROR_ON_MISSING_LIBRARIES=0 is passed'

    process = Popen([PYTHON, EMCC, path_from_root('tests', 'hello_world.cpp'), '-lsomenonexistingfile', '-s', 'STRICT=1', '-s', 'ERROR_ON_MISSING_LIBRARIES=0'], stdout=PIPE, stderr=PIPE, env=env)
    process.communicate()
    assert process.returncode is 0, '-s ERROR_ON_MISSING_LIBRARIES=0 should override -s STRICT=1'

    process = Popen([PYTHON, EMCC, path_from_root('tests', 'hello_world.cpp'), '-lsomenonexistingfile', '-s', 'STRICT=0'], stdout=PIPE, stderr=PIPE, env=env)
    process.communicate()
    # TODO: TEMPORARY: When -s ERROR_ON_MISSING_LIBRARIES=1 becomes the default, change the following line to expect failure instead of 0.
    assert process.returncode is 0, '-llsomenonexistingfile is not yet an error in non-strict mode'

  # Tests that if user accidentally attempts to link native object code, we show an error
  def test_native_link_error_message(self):
    run_process([CLANG, '-c', path_from_root('tests', 'hello_world.cpp'), '-o', 'hello_world.o'])
    err = run_process([PYTHON, EMCC, 'hello_world.o', '-o', 'hello_world.js'], stdout=PIPE, stderr=PIPE, check=False).stderr
    self.assertContained('hello_world.o is not valid LLVM bitcode', err)

  def test_o_level_clamp(self):
    for level in [3, 4, 20]:
      err = run_process([PYTHON, EMCC, '-O' + str(level), path_from_root('tests', 'hello_world.c')], stdout=PIPE, stderr=PIPE).stderr
      assert os.path.exists('a.out.js'), '-O' + str(level) + ' should produce output'
      if level > 3:
        self.assertContained("optimization level '-O" + str(level) + "' is not supported; using '-O3' instead", err)

  # Tests that if user specifies multiple -o output directives, then the last one will take precedence
  def test_multiple_o_files(self):
    Popen([PYTHON, EMCC, path_from_root('tests', 'hello_world.c'), '-o', 'a.js', '-o', 'b.js']).communicate()
    assert os.path.isfile('b.js')
    assert not os.path.isfile('a.js')

  # Tests that Emscripten-provided header files can be cleanly included in C code
  def test_include_system_header_in_c(self):
    for std in [[], ['-std=c89']]: # Test oldest C standard, and the default C standard
      for directory, headers in [
        ('emscripten', ['dom_pk_codes.h', 'em_asm.h', 'emscripten.h', 'fetch.h', 'html5.h', 'key_codes.h', 'threading.h', 'trace.h', 'vector.h', 'vr.h']), # This directory has also bind.h, val.h and wire.h, which require C++11
        ('AL', ['al.h', 'alc.h']),
        ('EGL', ['egl.h', 'eglplatform.h']),
        ('GL', ['freeglut_std.h', 'gl.h', 'glew.h', 'glfw.h', 'glu.h', 'glut.h']),
        ('GLES', ['gl.h', 'glplatform.h']),
        ('GLES2', ['gl2.h', 'gl2platform.h']),
        ('GLES3', ['gl3.h', 'gl3platform.h', 'gl31.h', 'gl32.h']),
        ('GLFW', ['glfw3.h']),
        ('KHR', ['khrplatform.h'])]:
        for h in headers:
          inc = '#include <' + directory + '/' + h + '>'
          print(inc)
          open('a.c', 'w').write(inc)
          open('b.c', 'w').write(inc)
          subprocess.check_call([PYTHON, EMCC] + std + ['a.c', 'b.c'])

  def test_single_file(self):
    for (single_file_enabled,
         meminit1_enabled,
         debug_enabled,
         emterpreter_enabled,
         emterpreter_file_enabled,
         closure_enabled,
         wasm_enabled,
         asmjs_fallback_enabled) in itertools.product([True, False], repeat=8):
      # skip unhelpful option combinations
      if (
          (asmjs_fallback_enabled and not wasm_enabled) or
          (emterpreter_file_enabled and not emterpreter_enabled)
      ):
        continue

      expect_wasm = wasm_enabled
      expect_emterpretify_file = emterpreter_file_enabled
      expect_meminit = (meminit1_enabled and not wasm_enabled) or (wasm_enabled and asmjs_fallback_enabled)
      expect_success = not (emterpreter_file_enabled and single_file_enabled)
      expect_asmjs_code = asmjs_fallback_enabled and wasm_enabled and not self.is_wasm_backend()
      expect_wast = debug_enabled and wasm_enabled and not self.is_wasm_backend()

      if self.is_wasm_backend() and (asmjs_fallback_enabled or emterpreter_enabled or not wasm_enabled):
        continue

      # currently, the emterpreter always fails with JS output since we do not preload the emterpreter file, which in non-HTML we would need to do manually
      should_run_js = expect_success and not emterpreter_enabled

      cmd = [PYTHON, EMCC, path_from_root('tests', 'hello_world.c')]

      if single_file_enabled:
        expect_asmjs_code = False
        expect_emterpretify_file = False
        expect_meminit = False
        expect_wasm = False
        expect_wast = False
        cmd += ['-s', 'SINGLE_FILE=1']
      if meminit1_enabled:
        cmd += ['--memory-init-file', '1']
      if debug_enabled:
        cmd += ['-g']
      if emterpreter_enabled:
        cmd += ['-s', 'EMTERPRETIFY=1']
      if emterpreter_file_enabled:
        cmd += ['-s', "EMTERPRETIFY_FILE='a.out.dat'"]
      if closure_enabled:
        cmd += ['--closure', '1']
      if wasm_enabled:
        method = 'native-wasm'
        if asmjs_fallback_enabled:
          method += ',asmjs'
        cmd += ['-s', 'WASM=1', '-s', "BINARYEN_METHOD='" + method + "'"]
      else:
        cmd += ['-s', 'WASM=0']

      print(' '.join(cmd))
      self.clear()
      proc = run_process(cmd, stdout=PIPE, stderr=STDOUT, check=False)
      print(os.listdir('.'))
      if expect_success and proc.returncode != 0:
        print(proc.stdout)
      assert expect_success == (proc.returncode == 0)
      assert expect_asmjs_code == os.path.exists('a.out.asm.js')
      assert expect_emterpretify_file == os.path.exists('a.out.dat')
      assert expect_meminit == (os.path.exists('a.out.mem') or os.path.exists('a.out.js.mem'))
      assert expect_wasm == os.path.exists('a.out.wasm')
      assert expect_wast == os.path.exists('a.out.wast')
      if should_run_js:
        self.assertContained('hello, world!', run_js('a.out.js'))

  def test_emar_M(self):
    open('file1', 'w').write(' ')
    open('file2', 'w').write(' ')
    subprocess.call([PYTHON, EMAR, 'cr', 'file1.a', 'file1'])
    subprocess.call([PYTHON, EMAR, 'cr', 'file2.a', 'file2'])
    emar = run_process([PYTHON, EMAR, '-M'], input='''create combined.a
addlib file1.a
addlib file2.a
save
end
''')
    result = run_process([PYTHON, EMAR, 't', 'combined.a'], stdout=PIPE).stdout
    assert 'file1' in result
    assert 'file2' in result


  def test_flag_aliases(self):
    def assert_aliases_match(flag1, flag2, flagarg, extra_args):
      results = {}
      for f in (flag1, flag2):
        outfile = 'aliases.js'
        subprocess.check_call([PYTHON, EMCC, path_from_root('tests', 'hello_world.c'), '-s', f + '=' + flagarg, '-o', outfile] + extra_args)
        with open(outfile) as out:
          results[f] = out.read()
      self.assertEqual(results[flag1], results[flag2], 'results should be identical')

    assert_aliases_match('WASM_MEM_MAX', 'BINARYEN_MEM_MAX', '16777216', ['-s', 'WASM=1'])

  def test_IGNORE_CLOSURE_COMPILER_ERRORS(self):
    open('pre.js', 'w').write(r'''
      // make closure compiler very very angry
      var dupe = 1;
      var dupe = 2;
      function Node() {
        throw 'Node is a DOM thing too, and use the ' + dupe;
      }
      function Node() {
        throw '(duplicate) Node is a DOM thing too, and also use the ' + dupe;
      }
    ''')
    def test(extra=[]):
      run_process([PYTHON, EMCC, path_from_root('tests', 'hello_world.c'), '-O2', '--closure', '1', '--pre-js', 'pre.js'] + extra)
    failed = False
    try:
      test()
    except:
      failed = True
    assert failed
    test(['-s', 'IGNORE_CLOSURE_COMPILER_ERRORS=1'])

  def test_toolchain_profiler(self):
    environ = os.environ.copy()
    environ['EM_PROFILE_TOOLCHAIN'] = '1'
    # replaced subprocess functions should not cause errors
    run_process([PYTHON, EMCC, path_from_root('tests', 'hello_world.c')], env=environ)

  def test_noderawfs(self):
    fopen_write = open(path_from_root('tests', 'asmfs', 'fopen_write.cpp'), 'r').read()
    open(os.path.join(self.get_dir(), 'main.cpp'), 'w').write(fopen_write)
    run_process([PYTHON, EMCC, os.path.join(self.get_dir(), 'main.cpp'), '-s', 'NODERAWFS=1'])
    self.assertContained("read 11 bytes. Result: Hello data!", run_js('a.out.js'))

    # NODERAWFS should directly write on OS file system
    self.assertEqual("Hello data!", open(os.path.join(self.get_dir(), 'hello_file.txt'), 'r').read())

  def test_noderawfs_disables_embedding(self):
    expected = '--preload-file and --embed-file cannot be used with NODERAWFS which disables virtual filesystem'
    base = [PYTHON, EMCC, path_from_root('tests', 'hello_world.c'), '-s', 'NODERAWFS=1']
    err = run_process(base + ['--preload-files', 'somefile'], stderr=PIPE, check=False).stderr
    assert expected in err
    err = run_process(base + ['--embed-files', 'somefile'], stderr=PIPE, check=False).stderr
    assert expected in err

  def test_autotools_shared_check(self):
    env = os.environ.copy()
    env['LC_ALL'] = 'C'
    expected = ': supported targets:.* elf'
    for python in [PYTHON, 'python', 'python2', 'python3']:
      try:
        out = run_process([python, EMCC, '--help'], stdout=PIPE, env=env).stdout
        assert re.search(expected, out)
      except OSError:
        # Ignore missing python aliases.
        pass

  def test_ioctl_window_size(self):
      self.do_other_test(os.path.join('other', 'ioctl', 'window_size'))

  def test_fd_closed(self):
    self.do_other_test(os.path.join('other', 'fd_closed'))

  def test_js_optimizer_parse_error(self):
    # check we show a proper understandable error for JS parse problems
    open('src.cpp', 'w').write(r'''
#include <emscripten.h>
int main() {
  EM_ASM({
    var x = !<->5.; // wtf
  });
}
''')
    output = run_process([PYTHON, EMCC, 'src.cpp', '-O2'], stdout=PIPE, stderr=PIPE, check=False)
    self.assertContained('''
var ASM_CONSTS = [function() { var x = !<->5.; }];
                                        ^
''', output.stderr)

  def test_wasm_sourcemap(self):
    # The no_main.c will be read (from relative location) due to speficied "-s"
    shutil.copyfile(path_from_root('tests', 'other', 'wasm_sourcemap', 'no_main.c'), 'no_main.c')
    wasm_map_cmd = [PYTHON, path_from_root('tools', 'wasm-sourcemap.py'),
                    '--sources', '--prefix', '=wasm-src:///',
                    '--dwarfdump-output',
                    path_from_root('tests', 'other', 'wasm_sourcemap', 'foo.wasm.dump'),
                    '-o', 'a.out.wasm.map',
                    path_from_root('tests', 'other', 'wasm_sourcemap', 'foo.wasm')]
    subprocess.check_call(wasm_map_cmd)
    output = open('a.out.wasm.map').read()
    # has "sources" entry with file (includes also `--prefix =wasm-src:///` replacement)
    self.assertIn('wasm-src:///no_main.c', output)
    # has "sourcesContent" entry with source code (included with `-s` option)
    self.assertIn('int foo()', output)
    # has some entries
    self.assertRegexpMatches(output, r'"mappings":\s*"[A-Za-z0-9+/]')

  def test_html_preprocess(self):
    test_file = path_from_root('tests', 'module', 'test_stdin.c')
    output_file = path_from_root('tests', 'module', 'test_stdin.html')
    shell_file = path_from_root('tests', 'module', 'test_html_preprocess.html')

    run_process([PYTHON, EMCC, '-o', output_file, test_file, '--shell-file', shell_file, '-s', 'NO_EXIT_RUNTIME=0'], stdout=PIPE, stderr=PIPE)
    output = open(output_file).read()
    self.assertContained("""T1:(else) NO_EXIT_RUNTIME != 1
T2:NO_EXIT_RUNTIME != 1
T3:NO_EXIT_RUNTIME < 2
T4:(else) NO_EXIT_RUNTIME <= 1
T5:(else) NO_EXIT_RUNTIME
T6:!NO_EXIT_RUNTIME""", output)

    run_process([PYTHON, EMCC, '-o', output_file, test_file, '--shell-file', shell_file, '-s', 'NO_EXIT_RUNTIME=1'], stdout=PIPE, stderr=PIPE)
    output = open(output_file).read()
    self.assertContained("""T1:NO_EXIT_RUNTIME == 1
T2:(else) NO_EXIT_RUNTIME == 1
T3:NO_EXIT_RUNTIME < 2
T4:(else) NO_EXIT_RUNTIME <= 1
T5:NO_EXIT_RUNTIME
T6:(else) !NO_EXIT_RUNTIME""", output)

    run_process([PYTHON, EMCC, '-o', output_file, test_file, '--shell-file', shell_file, '-s', 'NO_EXIT_RUNTIME=2'], stdout=PIPE, stderr=PIPE)
    output = open(output_file).read()
    self.assertContained("""T1:(else) NO_EXIT_RUNTIME != 1
T2:NO_EXIT_RUNTIME != 1
T3:(else) NO_EXIT_RUNTIME >= 2
T4:NO_EXIT_RUNTIME > 1
T5:NO_EXIT_RUNTIME
T6:(else) !NO_EXIT_RUNTIME""", output)

  # Tests that Emscripten-compiled applications can be run from a relative path with node command line that is different than the current working directory.
  def test_node_js_run_from_different_directory(self):
    if not os.path.exists('subdir'):
      os.mkdir('subdir')
    run_process([PYTHON, EMCC, path_from_root('tests', 'hello_world.c'), '-o', os.path.join('subdir', 'a.js'), '-O3'])
    ret = run_process(NODE_JS + [os.path.join('subdir', 'a.js')], stdout=PIPE).stdout
    self.assertContained('hello, world!', ret)

  def test_is_bitcode(self):
    fname = os.path.join(self.get_dir(), 'tmp.o')

    with open(fname, 'wb') as f:
      f.write(b'foo')
    self.assertFalse(Building.is_bitcode(fname))

    with open(fname, 'wb') as f:
      f.write(b'\xDE\xC0\x17\x0B')
      f.write(16 * b'\x00')
      f.write(b'BC')
    self.assertTrue(Building.is_bitcode(fname))

    with open(fname, 'wb') as f:
      f.write(b'BC')
    self.assertTrue(Building.is_bitcode(fname))

  def test_is_ar(self):
    fname = os.path.join(self.get_dir(), 'tmp.a')

    with open(fname, 'wb') as f:
      f.write(b'foo')
    self.assertFalse(Building.is_ar(fname))

    with open(fname, 'wb') as f:
      f.write(b'!<arch>\n')
    self.assertTrue(Building.is_ar(fname))
