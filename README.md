# .github

This is a repository for Travtus' workflow templates.

To include a new template, add it to the workflow-templates directory. This repository also has its own workflows
in its .github directory. This is for PR automation around the workflows themselves (e.g., automated comments). 

If you update this repository's template for automatic comments, then you should also update this repository's workflow
for automatic comments.

## Project configuration file

If you use `uv` to manage your Python projects, please copy `pyproject-uv.toml` to your project root and rename it to `pyproject.toml`.
This file contains the configuration for the `uv` tool, which is used to manage Python projects.

For other package management tools, like `pip`, `poetry`, you can use the `pyproject.toml` file as a reference to create your own configuration file.
