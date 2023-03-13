import sys
import os
import re
import argparse

if __name__== "__main__":
    new_release = ""
    parser = argparse.ArgumentParser()
    parser.add_argument('first_string')
    args = parser.parse_args()

    latest_version = re.search(r'\"tag_name\": \"v([0-9]+)\.([0-9]+)\.([0-9]+)', args.first_string)
    major = latest_version.group(1)
    minor = latest_version.group(2)
    patch = latest_version.group(3)

    new_release = f'v{major}.{str(int(minor) + 1)}.{patch}'

    with open(os.environ['GITHUB_OUTPUT'], 'a') as fh:
        print(f'latest-tag={new_release}', file=fh)
