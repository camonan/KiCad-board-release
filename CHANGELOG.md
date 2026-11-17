# Changelog

## 1.1.0

- The archive folder now defaults to the open project's own `archive/` folder
  (`<project>/archive/<revision>/`) instead of a path carried over from the last
  project. An existing `<project>/Archive` folder is still used.
- `archive_root` is saved relative to the project (blank for the default), so
  configs survive copying or moving a project. `{project}` and `{project_dir}`
  can be used in the path.
- A fixed absolute `archive_root` in the user-wide config is ignored unless it,
  or `folder_template`, includes `{project}`.

## 1.0.0

Initial release.
