# SPDX-FileCopyrightText: 2016 Jani Nikula <jani@nikula.org>
# SPDX-FileCopyrightText: 2018 Bruno Santos <brunomanuelsantos@tecnico.ulisboa.pt>
# SPDX-License-Identifier: BSD-2-Clause
"""
Documentation comment extractor
===============================

This module extracts relevant documentation comments.

This is the part that uses Clang Python Bindings to extract documentation
comments from C source code. This module does not depend on Sphinx.

There are two passes:

#. Pass over the tokens to find all the comments, including ones that aren't
   attached to cursors.

#. Pass over the cursors to document them.

There is minimal syntax parsing or input conversion:

* Identification of documentation comment blocks, i.e. comments that start
  with ``/**``.

* Identification of function-like macros.

* Identification of array and function pointer variables, members and
  arguments, and conversion to a format suitable for Sphinx C Domain.

The documentation comments are returned verbatim in a tree of Docstring objects.
"""

import enum
import os
from dataclasses import dataclass

from clang.cindex import (
    Index,
    TranslationUnit,
    TranslationUnitLoadError,
    Diagnostic,
)

from hawkmoth import docstring
from hawkmoth.doccursor import (
    CursorKind,
    TokenKind,
    DocCursor,
)

class ErrorLevel(enum.IntEnum):
    """
    Supported error levels. The values are an implementation detail.
    """
    DEBUG = Diagnostic.Ignored
    INFO = Diagnostic.Note
    WARNING = Diagnostic.Warning
    ERROR = Diagnostic.Error
    CRITICAL = Diagnostic.Fatal

@dataclass
class ParserError:
    level: ErrorLevel
    filename: str
    line: int
    message: str

    def get_message(self, basename=False):
        if self.filename:
            filename = os.path.basename(self.filename) if basename else self.filename

            if self.line is not None:
                return f'{filename}:{self.line}: {self.message}'
            else:
                return f'{filename}: {self.message}'
        else:
            return f'{self.message}'

def _domain_is_valid(tu, domain, errors):
    """Check the derived domain of a translation unit against the expected one.

    The derived domain is observed indirectly by the definition of certain C++
    specific macros. We try to maximize our chances by looking for any of the
    known macros in case any of them is disabled through compiler flags or
    preprocessor statements.
    """
    cpp_macros = (
        '__cpp_rtti',
        '__cpp_exceptions',
        '__cpp_unicode_characters',
        '__cpp_raw_strings',
        '__cpp_unicode_literals',
        '__cpp_user_defined_literals',
        '__cpp_lambdas',
        '__cpp_constexpr',
        '__cpp_constexpr_in_decltype',
        '__cpp_range_based_for',
        '__cpp_static_assert',
        '__cpp_decltype',
        '__cpp_attributes',
        '__cpp_rvalue_references',
        '__cpp_variadic_templates',
        '__cpp_initializer_lists',
        '__cpp_delegating_constructors',
        '__cpp_nsdmi',
        '__cpp_inheriting_constructors',
        '__cpp_ref_qualifiers',
        '__cpp_alias_templates',
        '__cpp_threadsafe_static_init',
        '__cpp_binary_literals',
        '__cpp_digit_separators',
        '__cpp_init_captures',
        '__cpp_generic_lambdas',
        '__cpp_decltype_auto',
        '__cpp_return_type_deduction',
        '__cpp_aggregate_nsdmi',
        '__cpp_variable_templates',
        '__cpp_impl_destroying_delete',
    )

    if domain not in ['c', 'cpp']:
        errors.append(ParserError(ErrorLevel.CRITICAL, None, None,
                                  f'domain \'{domain}\' not in [\'c\', \'cpp\']'))
        return False

    for cursor in tu.cursor.get_children():
        if cursor.kind == CursorKind.MACRO_DEFINITION and cursor.spelling in cpp_macros:
            if domain != 'cpp':
                errors.append(ParserError(ErrorLevel.CRITICAL, None, None,
                                          f'domain ({domain}) does not match inferred domain (cpp)'))  # noqa: E501
                return False
            return True

    if domain != 'c':
        errors.append(ParserError(ErrorLevel.CRITICAL, None, None,
                                  f'domain ({domain}) does not match inferred domain (c)'))  # noqa: E501
        return False
    return True

def _comment_extract(tu):

    # FIXME: How to handle top level comments above a cursor that it does *not*
    # describe? Parsing @file or @doc at this stage would not be a clean design.
    # One idea is to use '/***' to denote them, but that might throw off editor
    # highlighting. The workaround is to follow the top level comment with an
    # empty '/**/' comment that gets attached to the cursor.

    top_level_comments = []
    comments = {}
    current_leading_comment = None
    current_trailing_token = None

    def is_doc(cursor):
        return cursor and docstring.Docstring.is_doc(cursor.spelling)

    def is_trailing_doc(cursor):
        return cursor and docstring.Docstring.is_trailing_doc(cursor.spelling)

    class TokenType(enum.Enum):
        """
        Token types used in the parser
        """

        LEADING_COMMENT = enum.auto()
        TRAILING_COMMENT = enum.auto()
        SKIPPABLE = enum.auto()
        DOCUMENTABLE = enum.auto()
        SEPARATOR = enum.auto()
        UNKNOWN = enum.auto()

    eventsList = []
    for walkCursor in tu.cursor.walk_preorder():
        if walkCursor.location.file is None or walkCursor.location.file.name != tu.spelling:
            continue
        eventsList.append(("cursorstart", walkCursor, walkCursor.extent.start.line, walkCursor.extent.start.column))
        eventsList.append(("cursorend", walkCursor, walkCursor.extent.end.line, walkCursor.extent.end.column))

    for token in tu.get_tokens(extent=tu.cursor.extent):
        if token.kind != TokenKind.COMMENT:
            continue
        eventsList.append(("comment", token, token.extent.start.line, token.extent.start.column))

    eventsList.sort(key=lambda x: (x[2], x[3]))  # Sort by line and column

    for event in eventsList:
        if event[0] == "comment":
            token = event[1]
            if is_doc(token):
                token_type = TokenType.LEADING_COMMENT
            elif is_trailing_doc(token):
                token_type = TokenType.TRAILING_COMMENT
            else:
                token_type = TokenType.SEPARATOR
        elif event[0] == "cursorstart":
            if event[1].kind in [
                CursorKind.INVALID_FILE,
                CursorKind.TYPE_REF,
                CursorKind.TEMPLATE_REF,
                CursorKind.NAMESPACE_REF,
                CursorKind.PREPROCESSING_DIRECTIVE,
                CursorKind.MACRO_INSTANTIATION,
            ]:
                token_type = TokenType.SKIPPABLE
            elif event[1].kind in [CursorKind.LINKAGE_SPEC, CursorKind.UNEXPOSED_DECL]:
                token_type = TokenType.SEPARATOR
            else:
                token_type = TokenType.DOCUMENTABLE

        if token_type == TokenType.LEADING_COMMENT:
            if current_leading_comment is not None:
                top_level_comments.append(current_leading_comment)
            current_leading_comment = token
            current_trailing_token = None

        elif token_type == TokenType.TRAILING_COMMENT:
            if current_trailing_token is not None:
                if current_trailing_token.hash not in comments:
                    # Set the comment if no other comment has been set so far
                    comments[current_trailing_token.hash] = token

        elif token_type == TokenType.SEPARATOR:
            if current_leading_comment is not None:
                top_level_comments.append(current_leading_comment)
            current_leading_comment = None
            current_trailing_token = None

        elif token_type == TokenType.SKIPPABLE:
            pass  # Do nothing
        elif token_type == TokenType.DOCUMENTABLE:
            # Skip typedefs that apply to a struct, union, or enum, and retarget
            # trailing comments to the existing cursor. Otherwise, update the
            # current trailing token to the current cursor.
            if not (
                event[1].kind == CursorKind.TYPEDEF_DECL
                and current_trailing_token is not None
                and current_trailing_token.kind
                in [CursorKind.STRUCT_DECL, CursorKind.UNION_DECL, CursorKind.ENUM_DECL]
                and current_trailing_token.spelling == event[1].spelling
            ):
                current_trailing_token = event[1]

            # If we have a leading comment, it applies to this cursor.
            if current_leading_comment is not None:
                comments[event[1].hash] = current_leading_comment
                current_leading_comment = None

    # Unattached comment at the end of file.
    if current_leading_comment is not None:
        top_level_comments.append(current_leading_comment)

    return top_level_comments, comments


def _recursive_parse(errors, cursor, nest):

    if cursor.kind == CursorKind.MACRO_DEFINITION:

        if cursor.args is None:
            ds = docstring.MacroDocstring(cursor=cursor, nest=nest)
        else:
            ds = docstring.MacroFunctionDocstring(cursor=cursor, nest=nest)

        return [ds]

    elif cursor.kind == CursorKind.VAR_DECL:

        ds = docstring.VarDocstring(cursor=cursor, nest=nest)

        return [ds]

    elif cursor.kind == CursorKind.FIELD_DECL:

        ds = docstring.MemberDocstring(cursor=cursor, nest=nest)

        return [ds]

    elif cursor.kind == CursorKind.TYPEDEF_DECL:

        if cursor.is_function_pointer_typedef:
            ds = docstring.TypedefFunctionDocstring(cursor=cursor, nest=nest)
        else:
            ds = docstring.TypedefDocstring(cursor=cursor, nest=nest)

        return [ds]

    elif cursor.kind in [CursorKind.TYPE_ALIAS_DECL, CursorKind.TYPE_ALIAS_TEMPLATE_DECL]:

        ds = docstring.TypeAliasDocstring(cursor=cursor, nest=nest)

        return [ds]

    elif cursor.kind in [CursorKind.STRUCT_DECL,
                         CursorKind.UNION_DECL,
                         CursorKind.ENUM_DECL,
                         CursorKind.CLASS_DECL,
                         CursorKind.CLASS_TEMPLATE]:

        if cursor.kind == CursorKind.STRUCT_DECL:
            ds = docstring.StructDocstring(cursor=cursor, nest=nest)
        elif cursor.kind == CursorKind.UNION_DECL:
            ds = docstring.UnionDocstring(cursor=cursor, nest=nest)
        elif cursor.kind == CursorKind.ENUM_DECL:
            if cursor.is_scoped_enum:
                ds = docstring.EnumClassDocstring(cursor=cursor, nest=nest)
            else:
                ds = docstring.EnumDocstring(cursor=cursor, nest=nest)
        elif cursor.kind in [CursorKind.CLASS_DECL, CursorKind.CLASS_TEMPLATE]:
            ds = docstring.ClassDocstring(cursor=cursor, nest=nest)

        for c in cursor.get_children():
            if c.comment:
                ds.add_children(_recursive_parse(errors, c, nest + 1))

        return [ds]

    elif cursor.kind == CursorKind.ENUM_CONSTANT_DECL:

        ds = docstring.EnumeratorDocstring(cursor=cursor, nest=nest)

        return [ds]

    elif cursor.kind == CursorKind.FUNCTION_DECL:

        ds = docstring.FunctionDocstring(cursor=cursor, nest=nest)

        return [ds]

    elif cursor.kind in [CursorKind.CONSTRUCTOR,
                         CursorKind.DESTRUCTOR,
                         CursorKind.CXX_METHOD,
                         CursorKind.FUNCTION_TEMPLATE]:

        ds = docstring.FunctionDocstring(cursor=cursor, nest=nest)

        return [ds]

    # If we reach here, nothing matched i.e. there's a documentation comment
    # above an unexpected cursor.
    message = f'documentation comment attached to unexpected cursor {str(cursor.kind)} {cursor.name}'  # noqa: E501
    errors.append(ParserError(ErrorLevel.WARNING, cursor.location.file.name,
                              cursor.location.line, message))

    ds = docstring.TextDocstring(text=cursor.comment, meta=cursor.meta)

    return [ds]

def _clang_diagnostics(diagnostics, errors):
    for diag in diagnostics:
        filename = diag.location.file.name if diag.location.file else None
        errors.append(ParserError(ErrorLevel(diag.severity), filename,
                                  diag.location.line, diag.spelling))

def _parse_undocumented_block(errors, cursor, nest):
    """Parse undocumented blocks.

    Some blocks define plenty of children that may be documented themselves
    while the parent cursor itself has no documentation. One such example is the
    `extern "C"` block.
    """
    ret = []

    # Identify `extern "C"` and `extern "C++"` blocks and recursively parse
    # their contents.
    # Prior to Clang 18, the Python bindings don't return the cursor kind
    # LINKAGE_SPEC as one would expect, so we need to do it the hard way.
    if cursor.kind in [CursorKind.LINKAGE_SPEC, CursorKind.UNEXPOSED_DECL]:
        tokens = cursor.get_tokens()
        ntoken = next(tokens, None)
        if ntoken and ntoken.spelling == 'extern':
            ntoken = next(tokens, None)

            if not ntoken:
                return ret

            if ntoken.spelling not in ['"C"', '"C++"']:
                message = f'unhandled `extern {ntoken.spelling}` block will mask all children'
                errors.append(ParserError(ErrorLevel.WARNING,
                                          cursor.location.file.name,
                                          cursor.location.line, message))
                return ret

            for c in cursor.get_children():
                if c.comment:
                    ret.extend(_recursive_parse(errors, c, nest))

    elif cursor.kind == CursorKind.NAMESPACE:
        # ignore internal STL namespaces
        if cursor.name in ['std', '__gnu_cxx', '__cxxabiv1', '__gnu_debug']:
            return ret
        # iterate over namespace
        for c in cursor.get_children():
            if c.comment:
                ret.extend(_recursive_parse(errors, c, nest))
            else:
                ret.extend(_parse_undocumented_block(errors, c, nest))

    return ret

def _language_option(filename, domain):
    """Return clang -x<language> option depending on domain and filename."""
    if domain == 'cpp':
        language = '-xc++'
    else:
        language = '-xc'

    if os.path.splitext(filename)[1] in ['.h', '.H', '.hh', '.hpp', '.hxx']:
        language += '-header'

    return language

# Parse a file and return a tree of docstring.Docstring objects.
def parse(filename, domain=None, clang_args=None):
    # Empty root comment with just children
    result = docstring.RootDocstring(filename=filename, domain=domain,
                                     clang_args=clang_args)
    errors = []
    index = Index.create()

    # Note: Preserve the passed in clang_args in RootDocstring, as it's used for
    # filtering by the callers
    full_args = [_language_option(filename, domain)]
    if clang_args:
        full_args.extend(clang_args)

    try:
        tu = index.parse(filename, args=full_args,
                         options=TranslationUnit.PARSE_DETAILED_PROCESSING_RECORD |
                         TranslationUnit.PARSE_SKIP_FUNCTION_BODIES)
    except TranslationUnitLoadError as e:
        # File not found is a common problem, but not properly reported by
        # clang. Try to be a bit more helpful.
        if not os.path.isfile(filename):
            message = f'File not found. {str(e)}'
        else:
            message = str(e)

        errors.append(ParserError(ErrorLevel.CRITICAL, filename, None, message))

        return result, errors

    _clang_diagnostics(tu.diagnostics, errors)

    if not _domain_is_valid(tu, domain, errors):
        return result, errors

    top_level_comments, comments = _comment_extract(tu)

    for comment in top_level_comments:
        text = comment.spelling
        meta = {'line': comment.extent.start.line}
        ds = docstring.TextDocstring(text=text, meta=meta)
        result.add_child(ds)

    for cc in tu.cursor.get_children():
        cursor = DocCursor(domain=domain, cursor=cc, comments=comments)
        if cursor.comment:
            result.add_children(_recursive_parse(errors, cursor, 0))
        else:
            result.add_children(_parse_undocumented_block(errors, cursor, 0))

    return result, errors
