from pathlib import Path
from typing import cast

from PySide6.QtCore import QCoreApplication, Qt
from PySide6.QtWidgets import QApplication, QDialog, QHBoxLayout, QLabel, QPushButton, QVBoxLayout


# noinspection PyAttributeOutsideInit
class AboutDialog(QDialog):
    app: QApplication

    def __init__(self, parent=None) -> None:
        super().__init__(parent)

        app = cast(QApplication, QApplication.instance())
        if not app:
            raise ValueError("QApplication instance not found.")

        self.app = app
        self.readme_url = self.app.property("url")

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
            name, _, email = author.partition("<")
            email = email.rstrip(">")
            if email:
                authors_str += f"{name} <a href='mailto:{email}'>{email}</a> "
            else:
                authors_str += f"{name} "

        authors_label = QLabel(f"Author(s):<br />{authors_str}")
        authors_label.setTextFormat(Qt.TextFormat.RichText)

        authors_label.setOpenExternalLinks(True)
        authors_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(authors_label)

    def add_readme_label(self, layout: QVBoxLayout) -> None:
        if not self.readme_url:
            return

        readme_label = QLabel(f"<a href='{self.readme_url}'>Readme</a>")
        readme_label.setTextFormat(Qt.TextFormat.RichText)
        readme_label.setOpenExternalLinks(True)
        readme_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(readme_label)

    def add_description_label(self, layout: QVBoxLayout) -> None:
        description_label = QLabel(self.get_section_from_readme("Introduction"))
        description_label.setWordWrap(True)
        description_label.setTextFormat(Qt.TextFormat.MarkdownText)
        description_label.setMaximumWidth(800)
        layout.addWidget(description_label)

    def add_close_button(self, layout: QVBoxLayout) -> None:
        button_layout = QHBoxLayout()
        button_layout.addStretch(1)
        close_button = QPushButton("Close", self)
        close_button.clicked.connect(self.close)
        button_layout.addWidget(close_button)
        button_layout.addStretch(1)
        layout.addWidget(close_button)

    def get_section_from_readme(self, section: str) -> str:
        if not (readme_path := self.find_readme()):
            return "No README found."

        readme_lines = readme_path.read_text().splitlines()

        try:
            start = readme_lines.index(f"## {section}") + 1
            end = start
            while end < len(readme_lines) and not readme_lines[end].startswith("## "):
                end += 1
        except ValueError:
            start = 0
            end = len(readme_lines)

        section_lines = ["  \n" if line == "" else line for line in readme_lines[start:end]]

        return " ".join(section_lines)

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
