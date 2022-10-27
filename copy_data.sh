#!/bin/bash

# This is used to copy over data for development purposes
# Make sure to put your tables in ./data
docker container create --name temp -v data:/data hello-world
docker cp ./data temp:/data
docker rm temp
