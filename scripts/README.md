# Double-click launchers

The same commands as `jobapp.cmd` and `jobapp.sh`, for people who prefer double-clicking.

- **Windows:** open the `Windows` folder and double-click `1 Set up.bat`, then `2 Sign in.bat`, then `3 Check this computer.bat`. After that, `Start Job App.bat` is the only one you need.
- **Mac:** do the same with the `.command` files in the `Mac` folder. Each one opens a Terminal window.
  - If the Mac says a file "could not be executed because you do not have appropriate access privileges", run this once in Terminal, in the Job App folder: `chmod +x scripts/Mac/*.command`
  - If it says the file is from an unidentified developer, right-click it, choose **Open**, then confirm.

`Start Job App` keeps its window open while Job App runs. Close that window, or press Ctrl+C in it, to stop Job App.
