"""Optional OpenCV crowd monitor. Run: python cv/people_counter.py. Press Q to quit."""
import cv2
hog=cv2.HOGDescriptor(); hog.setSVMDetector(cv2.HOGDescriptor_getDefaultPeopleDetector())
cap=cv2.VideoCapture(0)
if not cap.isOpened(): raise SystemExit('Could not open the camera. Check camera permissions.')
while True:
    ok,frame=cap.read()
    if not ok: break
    frame=cv2.resize(frame,None,fx=.8,fy=.8)
    boxes,weights=hog.detectMultiScale(frame,winStride=(8,8),padding=(8,8),scale=1.05)
    count=len(boxes)
    level='OVERCROWDED' if count>=15 else 'HIGH' if count>=10 else 'MEDIUM' if count>=5 else 'LOW'
    for x,y,w,h in boxes: cv2.rectangle(frame,(x,y),(x+w,y+h),(255,255,255),2)
    cv2.putText(frame,f'People detected: {count}',(20,35),cv2.FONT_HERSHEY_SIMPLEX,.8,(255,255,255),2)
    cv2.putText(frame,f'Queue level: {level}',(20,70),cv2.FONT_HERSHEY_SIMPLEX,.8,(255,255,255),2)
    cv2.imshow('SmartQueue - OpenCV Crowd Monitor',frame)
    if cv2.waitKey(1)&0xFF==ord('q'): break
cap.release(); cv2.destroyAllWindows()
