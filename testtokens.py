import argparse
from clang.cindex import (
    Index,
    TranslationUnit,
    TranslationUnitLoadError,
    Diagnostic,
    TokenKind,
    CursorKind,
)
from textwrap import indent

from blessed import Terminal

import timeit
import cProfile

from pprint import pprint

t = Terminal()

def get_event_list(tu, cursor, level=0):
    eventsList = []
    cursors = cursor.walk_preorder()

    for walkCursor in cursors:
        if walkCursor.location.file is None or walkCursor.location.file.name != tu.spelling:
            continue
        eventsList.append(("cursorstart", walkCursor, level, walkCursor.extent.start.line, walkCursor.extent.start.column))
        eventsList.append(("cursorend", walkCursor, level, walkCursor.extent.end.line, walkCursor.extent.end.column))

    allTokens = list(tu.get_tokens(extent=cursor.extent))
    for token in allTokens:
        if token.kind != TokenKind.COMMENT:
            continue
        eventsList.append(("token", token, level, token.extent.start.line, token.extent.start.column))

    eventsList.sort(key=lambda x: (x[3], x[4]))  # Sort by line and column
    return eventsList

def main():
    argparser = argparse.ArgumentParser(description="Test reading tokens from a file.")
    argparser.add_argument("filepath", type=str, help="Path to the file to read tokens from.")
    args = argparser.parse_args()

    index = Index.create()

    tu = index.parse(
        args.filepath,
        options=TranslationUnit.PARSE_DETAILED_PROCESSING_RECORD |
                         TranslationUnit.PARSE_SKIP_FUNCTION_BODIES
    )

    print(t.bold("Handle Cursor 2:"))
    pr = cProfile.Profile()
    pr.enable()
    d = get_event_list(tu, tu.cursor)
    pr.disable()
    pr.dump_stats("handle_cursor_2.prof")
    pr.print_stats(sort="cumtime")
    print("\n\n\n")

    level = 0
    for event in d:
        if event[0] == "cursorstart":
            print(f"{'  ' * level}Cursor Start: {event[1].spelling} {event[1].kind}")
            level += 1
        elif event[0] == "cursorend":
            level -= 1
            # print(f"{'  ' * level}Cursor End: {event[1].spelling}")
        elif event[0] == "token":
            print(t.green(f"{indent(event[1].spelling, '  ' * (level))}"))

    # prevCursor = None
    # prevLevel = 0
    # for (token, cursor, level) in tokensList3:
    #     if token.kind == TokenKind.PUNCTUATION:
    #         continue

    #     if token.kind == TokenKind.COMMENT:
    #         print(t.green(indent(token.spelling, "  " * (level + 1))))

    #     if prevCursor is not None:
    #         if prevLevel != level:
    #             if cursor.kind != CursorKind.TRANSLATION_UNIT:
    #                 print(f"{'  ' * level}{cursor.spelling:<40}")
    #     prevCursor = cursor
    #     prevLevel = level


if __name__ == "__main__":
    main()