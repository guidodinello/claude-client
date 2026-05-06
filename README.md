lets implement a claude client like module to interact with the claude web api. upload, download, sync project files, etc.

also download the project memory into a file, could be like an export project feature where we download a web claude project (generated memory + project knowledge files + prompt/instructions + title + description) into an md file based format.

we already have a few implementations that in the future we could deprecate and use this new module instead.

this could be like a python module that we can import and use in our other scripts.

take a look at:

- @/home/guido/projects/weekly-highlights/clients/claude_uploader.py
- @/home/guido/projects/knowledger/knowledger/claude_client.py