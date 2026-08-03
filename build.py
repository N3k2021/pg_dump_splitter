import python_minifier

with open('main.py', 'r') as f:
    code = f.read()

# Убирает комментарии, docstrings, лишние пробелы
minified = python_minifier.minify(
    code,
    remove_annotations=True,
    remove_pass=True,
    remove_literal_statements=True,
    remove_asserts=True,
    remove_debug=True,
    hoist_literals=False
)

with open('main_minified.py', 'w') as f:
    f.write(minified)