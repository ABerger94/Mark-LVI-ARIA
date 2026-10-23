import faulthandler, sys
faulthandler.enable()
import PyQt6.QtCore as C
from PyQt6.QtWidgets import QApplication, QGraphicsScene, QGraphicsView
from PyQt6.QtCore import QUrl, QTimer
from PyQt6.QtMultimedia import QMediaPlayer, QAudioOutput
from PyQt6.QtMultimediaWidgets import QGraphicsVideoItem
print("Qt:", C.qVersion())
app = QApplication(sys.argv)
item = QGraphicsVideoItem()
scene = QGraphicsScene(); scene.addItem(item)
view = QGraphicsView(scene); view.resize(640, 360); view.show()
player = QMediaPlayer(); player.setVideoOutput(item)
player.setAudioOutput(QAudioOutput())
player.errorOccurred.connect(lambda *a: print("ERROR:", player.errorString()))
player.playbackStateChanged.connect(lambda s: print("STATE:", s))
player.setSource(QUrl.fromLocalFile(sys.argv[1]))
player.play()
QTimer.singleShot(4000, app.quit)
app.exec()
print("SURVIVED")
