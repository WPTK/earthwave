"""Read the current iTerm2 session screen via the Python API (async_get_screen_contents)."""

import iterm2


async def main(connection):
    app = await iterm2.async_get_app(connection)
    session = app.current_window.current_tab.current_session
    contents = await session.async_get_screen_contents()
    print("\n".join(contents.line(i).string for i in range(contents.number_of_lines)))


iterm2.run_until_complete(main)
