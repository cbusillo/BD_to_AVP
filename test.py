from github import Github

repo = Github().get_repo("cbusillo/bd_to_avp")
releases = repo.get_releases()
total_downloads = 0

print("Release Statistics for 'cbusillo/bd_to_avp':\n")

for release in releases:
    print(f"Release Tag: {release.tag_name}")
    downloads = 0
    for asset in release.get_assets():
        downloads += asset.download_count
    print(f"Download Count: {downloads}")
    print("-" * 30)

    total_downloads += downloads

print(f"\nTotal Downloads: {total_downloads}")
