import pathlib

root = pathlib.Path(__file__).resolve().parent
out_path = root / 'fvsx_all_text_files_summary.txt'
text_exts = {'.py', '.html', '.json', '.txt', '.md', '.csv', '.ini', '.cfg', '.yml', '.yaml', '.js', '.css', '.xml', '.sql'}
ignore_dirs = {'__pycache__'}
count = 0
failed = []

with out_path.open('w', encoding='utf-8') as out:
    out.write('FVSX READABLE FILES SUMMARY\n')
    out.write(f'Generated from all files under {root}\n\n')
    for path in sorted(root.rglob('*')):
        if path.is_dir():
            continue
        if any(part in ignore_dirs for part in path.parts):
            continue
        ext = path.suffix.lower()
        if ext in text_exts or path.name in {'requirements.txt', 'Admin.html', 'user.html'}:
            try:
                text = path.read_text(encoding='utf-8')
            except Exception:
                try:
                    text = path.read_text(encoding='latin-1')
                except Exception as exc:
                    failed.append(str(path))
                    continue
            count += 1
            out.write(f'--- FILE: {path} ---\n')
            out.write(f'SIZE: {path.stat().st_size} bytes\n')
            out.write(text)
            out.write('\n\n')
    out.write(f'TOTAL_READABLE_FILES: {count}\n')
    if failed:
        out.write('FAILED_TO_READ:\n')
        for item in failed:
            out.write(item + '\n')

print(out_path)
