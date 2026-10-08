import os
import unittest
from collections.abc import Callable

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QCoreApplication, QEvent
from PySide6.QtWidgets import QApplication, QVBoxLayout, QWidget
from shiboken6 import isValid

from bd_to_avp.gui.widget import LabeledComboBox, LabeledLineEdit, LabeledSpinBox


class LabeledWidgetOwnershipTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def factories(self) -> tuple[Callable[..., QWidget], ...]:
        return (
            lambda **kwargs: LabeledComboBox("Choice", ["First", "Second"], default_value="Second", **kwargs),
            lambda **kwargs: LabeledLineEdit("Text", default_value="Value", **kwargs),
            lambda **kwargs: LabeledSpinBox("Number", default_value=42, **kwargs),
        )

    def delete_widget(self, widget: QWidget) -> None:
        widget.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)

    def test_parent_destruction_releases_labeled_controls(self) -> None:
        for factory in self.factories():
            with self.subTest(factory=factory):
                parent = QWidget()
                control = factory(parent=parent)
                self.delete_widget(parent)
                try:
                    self.assertFalse(isValid(control))
                finally:
                    if isValid(control):
                        self.delete_widget(control)

    def test_parent_with_existing_content_keeps_and_owns_both_controls(self) -> None:
        for factory in self.factories():
            with self.subTest(factory=factory):
                parent = QWidget()
                layout = QVBoxLayout(parent)
                sibling = LabeledLineEdit("Existing", default_value="Keep")
                layout.addWidget(sibling)
                control = factory(parent=parent)
                try:
                    self.assertEqual(sibling.text(), "Keep")
                    self.assertIs(sibling.parentWidget(), parent)
                    self.assertIs(control.parentWidget(), parent)
                finally:
                    self.delete_widget(parent)
                    if isValid(control):
                        self.delete_widget(control)

    def test_parentless_controls_can_be_embedded_with_initial_values(self) -> None:
        parent = QWidget()
        layout = QVBoxLayout(parent)
        combo, line, spin = [factory() for factory in self.factories()]
        try:
            for control in (combo, line, spin):
                layout.addWidget(control)
            self.assertEqual(combo.current_text(), "Second")
            self.assertEqual(line.text(), "Value")
            self.assertEqual(spin.value(), 42)
        finally:
            self.delete_widget(parent)
        for control in (combo, line, spin):
            self.assertFalse(isValid(control))


if __name__ == "__main__":
    unittest.main()
