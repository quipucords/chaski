# chaski
chaski mission is to help delivering upstream code to downstream build system.

## Creating a downstream build

Before running chaski, you will need to: 
- clone discovery-server from `dist-git`
- switch to an appropriate branch (or create a new one prefixed by `private-` if you are experimenting)
- create/edit a file named `sources-version.yaml`. It should look like this
    ```yaml
    bootstrap-yarn: main
    qpc: 1.0.1
    quipucords-server: 1.0.2
    quipucords-ui: 1.0.0
    ```
    sources-version.yaml maps *commitshes* such as tags, branches, shortened commit shas
    to sources that are present on `container.yaml` (more info about it on [OSBS docs](https://osbs.readthedocs.io/en/latest/users.html#fetching-source-code-from-external-source-using-cachito)). 
    
    Edit the versions you want to use (on upstream we usually ship human readable github releases).

    It's propably a good idea to keep [bootstrap-yarn](https://github.com/quipucords/bootstrap-yarn) pointing to main.

After this setup, just run `chaski update-remote-sources path/to/discovery-server`. Chaski will print a message reminding 
you what's the next step for creating a downstream build.

## What does chaski means?

*chaski* are the postal messengers from [ancient Inca empire](https://www.worldhistory.org/Quipu/).
