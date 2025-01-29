from django.db import models


class ActiveLoop(models.Model):
    mint = models.CharField(max_length=100)
    loop_time = models.IntegerField()

    def __str__(self):
        return f"{self.mint} (Every {self.loop_time} sec)"
