from pathlib import Path
from typing import cast
from urllib.parse import urlparse

from PySide6.QtCore import QCoreApplication, Qt
from PySide6.QtWidgets import QApplication, QDialog, QHBoxLayout, QLabel, QPushButton, QVBoxLayout
from github import Github, GithubException


class AboutDialog(QDialog):
    app: QApplication

    def __init__(self, parent=None) -> None:
        super().__init__(parent)

        app = cast(QApplication, QApplication.instance())
        if not app:
            raise ValueError("QApplication instance not found.")

        self.app = app

        self.setWindowTitle(f"About {self.app.applicationDisplayName()}")
        self.create_dialog()

    def create_dialog(self) -> None:
        layout = QVBoxLayout()

        self.add_logo(layout)
        self.add_name_label(layout)
        self.add_version_label(layout)
        self.add_company_label(layout)
        self.add_authors_label(layout)
        self.add_readme_label(layout)
        self.add_update_label(layout)
        self.add_description_label(layout)
        self.add_close_button(layout)

        self.setLayout(layout)

    def add_logo(self, layout: QVBoxLayout) -> None:
        logo = QLabel()
        logo.setAlignment(Qt.AlignmentFlag.AlignCenter)
        logo.setPixmap(self.app.windowIcon().pixmap(128, 128))
        layout.addWidget(logo)

    def add_name_label(self, layout: QVBoxLayout) -> None:
        name_label = QLabel(self.app.applicationDisplayName())
        name_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(name_label)

    def add_version_label(self, layout: QVBoxLayout) -> None:
        version_label = QLabel(f"Version: {self.app.applicationVersion()}")
        version_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(version_label)

    def add_company_label(self, layout: QVBoxLayout) -> None:
        company_label = QLabel(f"By: {self.app.organizationName()}")
        company_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(company_label)

    def add_authors_label(self, layout: QVBoxLayout) -> None:
        authors = self.app.property("authors")
        if not authors:
            return
        authors_str = ""
        for author in authors:
            name, email = author.split("<")
            email = email.rstrip(">")
            if email:
                authors_str += f"{name} <a href='mailto:{email}'>{email}</a> "
            else:
                authors_str += f"{name} "

        authors_label = QLabel(f"Authors:<br />{authors_str}")
        authors_label.setTextFormat(Qt.TextFormat.RichText)

        authors_label.setOpenExternalLinks(True)
        authors_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(authors_label)

    def add_readme_label(self, layout: QVBoxLayout) -> None:
        readme_url = self.app.property("url")
        if not readme_url:
            return

        readme_label = QLabel(f"<a href='{readme_url}'>Readme</a>")
        readme_label.setTextFormat(Qt.TextFormat.RichText)
        readme_label.setOpenExternalLinks(True)
        readme_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(readme_label)

    def add_update_label(self, layout: QVBoxLayout) -> None:
        readme_url = self.app.property("url")
        if not readme_url:
            return

        update_label = QLabel()
        update_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        update_label.setTextFormat(Qt.TextFormat.RichText)
        update_label.setOpenExternalLinks(True)
        try:
            github = Github()
            repo_name = urlparse(readme_url).path.lstrip("/")
            repo = github.get_repo(repo_name)
            releases = repo.get_releases()
            latest_release = releases[0]
            latest_release_version = latest_release.tag_name.lstrip("v")
            latest_release_url = latest_release.html_url
            if latest_release_version != self.app.applicationVersion():
                update_label.setText(
                    f"New version available: <a href='{latest_release_url}'>v{latest_release_version}</a>"
                )

            else:
                update_label.setText(f"You are using the latest <a href='{latest_release_url}'>version</a>.")
        except GithubException:
            update_label.setText("Failed to check for updates.")

        layout.addWidget(update_label)

    def add_description_label(self, layout: QVBoxLayout) -> None:
        description_label = QLabel(self.get_description_from_readme())
        layout.addWidget(description_label)

    def add_close_button(self, layout: QVBoxLayout) -> None:

        button_layout = QHBoxLayout()
        button_layout.addStretch(1)
        close_button = QPushButton("Close", self)
        close_button.clicked.connect(self.close)
        button_layout.addWidget(close_button)
        button_layout.addStretch(1)
        layout.addWidget(close_button)

    def get_description_from_readme(self) -> str:
        if not (readme_path := self.find_readme()):
            return "No README found."

        readme_lines = readme_path.read_text().splitlines()

        try:
            start = readme_lines.index("## Introduction") + 1
            end = start
            while end < len(readme_lines) and not readme_lines[end].startswith("## "):
                end += 1
        except ValueError:
            start = 0
            end = len(readme_lines)

        return "\n".join(readme_lines[start:end])

    @staticmethod
    def find_readme() -> Path | None:
        current_path = Path(__file__).parent.parent
        readme_paths = [
            Path(QCoreApplication.applicationDirPath()) / "README.md",
            current_path / "README.md",
            current_path.parent / "README.md",
            current_path.parent.parent / "README.md",
        ]

        for current_path in readme_paths:
            if current_path.exists() and current_path.is_file():
                return current_path
        return None
