import sys
import unittest
from unittest import mock
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "launcher"))
import basara4_launcher as launcher  # noqa: E402


class LauncherCloseTest(unittest.TestCase):
    def owner(self, process=None):
        owner = launcher.Launcher.__new__(launcher.Launcher)
        owner.proc = process
        owner._cleanup_update_view = mock.Mock()
        owner._collect = mock.Mock()
        owner.destroy = mock.Mock()
        return owner

    def test_closing_with_a_running_game_detaches_without_killing_it(self):
        process = mock.Mock()
        process.poll.return_value = None
        owner = self.owner(process)
        with mock.patch.object(launcher.messagebox, "askyesno", return_value=True) as confirm:
            owner._close()

        self.assertIn("leave the game running", confirm.call_args.args[1])
        process.kill.assert_not_called()
        self.assertIsNone(owner.proc)
        owner._cleanup_update_view.assert_called_once()
        owner._collect.assert_called_once()
        owner.destroy.assert_called_once()

    def test_cancel_keeps_launcher_and_game_open(self):
        process = mock.Mock()
        process.poll.return_value = None
        owner = self.owner(process)
        with mock.patch.object(launcher.messagebox, "askyesno", return_value=False):
            owner._close()

        self.assertIs(owner.proc, process)
        process.kill.assert_not_called()
        owner._cleanup_update_view.assert_not_called()
        owner._collect.assert_not_called()
        owner.destroy.assert_not_called()

    def test_closing_without_a_running_game_does_not_prompt(self):
        owner = self.owner()
        with mock.patch.object(launcher.messagebox, "askyesno") as confirm:
            owner._close()

        confirm.assert_not_called()
        owner._cleanup_update_view.assert_called_once()
        owner._collect.assert_called_once()
        owner.destroy.assert_called_once()

    def test_play_mode_exits_after_starting_game_without_terminating_it(self):
        process = mock.Mock()
        owner = self.owner(process)
        owner._exit_after_play_launch()

        process.kill.assert_not_called()
        self.assertIsNone(owner.proc)
        owner.destroy.assert_called_once()


if __name__ == "__main__":
    unittest.main()
